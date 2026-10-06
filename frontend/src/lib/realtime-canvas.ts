// Wire framing and send policy for the realtime drawing canvas (issue #3).
// Kept apart from the panel so node --test can exercise framing and the live
// session lifecycle without loading Svelte. The panel owns the DOM and controls.
//
// The wire is docs/connection-handling.md: a 21 byte header of one kind byte,
// the 16 byte session UUID and a 4 byte big endian revision, then a complete
// WebP image. A canvas frame carries the session's own input count and a
// generated frame echoes the revision of the input that produced it, so an
// output below the revision already shown is dropped instead of drawn. ready
// also carries the session's limits, and a canvas image above
// max_frame_bytes is never framed.

/** Browser to worker. */
export const CANVAS_FRAME = 0x01;
/** Worker to browser. */
export const GENERATED_FRAME = 0x02;
/** One kind byte, the 16 byte session UUID, then a 4 byte big endian revision. */
export const FRAME_HEADER_BYTES = 21;
/** Where the revision sits in the header, after the kind byte and the UUID. */
const REVISION_OFFSET = 17;

/** Issue #3 asks for 2 to 4 fps. Finer adaptation is issue #42's. */
export const FAST_INTERVAL_MS = 250;
export const SLOW_INTERVAL_MS = 500;

/** Idle time with nothing to send before the capture loop stops arming itself.
 * A tick count would make the deadline depend on the interval it is armed
 * with: the same eight ticks are 2 s at the fast end of the band and 4 s at
 * the slow one. */
export const IDLE_STOP_MS = 1500;

const UUID_HEX = /^[0-9a-f]{32}$/i;

/** The 16 raw bytes of a canonical UUID string. */
export function uuidBytes(id: string): Uint8Array {
	const hex = id.replace(/-/g, '');
	if (!UUID_HEX.test(hex)) throw new Error(`not a uuid: ${id}`);
	const bytes = new Uint8Array(16);
	for (let index = 0; index < 16; index += 1) {
		bytes[index] = Number.parseInt(hex.slice(index * 2, index * 2 + 2), 16);
	}
	return bytes;
}

/**
 * A complete canvas frame ready for socket.send.
 *
 * `revision` is the session's input count, written big endian over four
 * bytes: a 16 bit write would wrap long before a page reload, and a little
 * endian one would read back as a different number on the API's side.
 *
 * The buffer is named in the type because send and the Blob constructor both
 * reject the ArrayBufferLike a bare Uint8Array widens to.
 */
export function canvasFrame(
	sessionId: string,
	revision: number,
	image: Uint8Array
): Uint8Array<ArrayBuffer> {
	const frame = new Uint8Array(FRAME_HEADER_BYTES + image.length);
	frame[0] = CANVAS_FRAME;
	frame.set(uuidBytes(sessionId), 1);
	new DataView(frame.buffer).setUint32(REVISION_OFFSET, revision, false);
	frame.set(image, FRAME_HEADER_BYTES);
	return frame;
}

/** The revision a generated frame was cut for, and its image bytes. */
export interface GeneratedFrame {
	revision: number;
	image: Uint8Array<ArrayBuffer>;
}

/**
 * The revision and image bytes of a generated frame, or null when the frame
 * is not one: too short, the wrong kind, or another session's. The whole UUID
 * is compared because a frame from a session that differs in one byte is
 * still not ours.
 */
export function parseGeneratedFrame(
	data: Uint8Array<ArrayBuffer>,
	sessionId: string
): GeneratedFrame | null {
	if (data.length < FRAME_HEADER_BYTES) return null;
	if (data[0] !== GENERATED_FRAME) return null;
	const expected = uuidBytes(sessionId);
	for (let index = 0; index < 16; index += 1) {
		if (data[index + 1] !== expected[index]) return null;
	}
	const revision = new DataView(data.buffer, data.byteOffset, data.byteLength).getUint32(
		REVISION_OFFSET,
		false
	);
	return { revision, image: data.subarray(FRAME_HEADER_BYTES) };
}

/**
 * Whether a generated frame is older than the one already on screen.
 *
 * Strictly below, so an equal revision is drawn: one input may be rendered
 * more than once, and dropping it would leave the output stuck on a worker
 * that repeats a revision. The comparison is a function because the arrival
 * check and the drain loop have to agree on exactly this line.
 */
export function isStaleOutput(revision: number, shownRevision: number): boolean {
	return revision < shownRevision;
}

/**
 * Whether to encode and send a frame now.
 *
 * `buffered` is the socket's bufferedAmount. Holding off while anything is
 * still queued is what keeps a slow uplink from compounding: every frame is a
 * complete canvas, so a backlog delivers stale images in order and the lag
 * never recovers. One encode in flight bounds the CPU, this bounds the wire.
 */
export function shouldSendFrame(state: {
	changed: boolean;
	encoding: boolean;
	buffered: number;
}): boolean {
	return state.changed && !state.encoding && state.buffered === 0;
}

/**
 * Whether the capture loop has been idle long enough to stop arming itself.
 *
 * `idleMs` is the sum of the intervals already armed, never a reading of a
 * clock: the injected setTimeout is the only time source the loop has, so the
 * deadline moves in tests exactly as it moves in a browser. A pending change
 * holds the loop open past the deadline, because a loop that stopped on a
 * change would strand the drawing until the next one arrived.
 */
export function shouldStopPolling(idleMs: number, changed: boolean): boolean {
	return idleMs >= IDLE_STOP_MS && !changed;
}

/**
 * The period to aim for between frame starts. Backs off to the slow end of the
 * band when encoding and queueing a frame already costs more than the fast
 * interval. This measures the browser's own cost, not the model's: revisions
 * correlate a generated frame with the input that produced it, but this period
 * is still fed only the browser's own encode and queue cost.
 */
export function nextIntervalMs(lastFrameCostMs: number): number {
	return lastFrameCostMs > FAST_INTERVAL_MS ? SLOW_INTERVAL_MS : FAST_INTERVAL_MS;
}

/**
 * How long to wait before starting the next frame, given what the last one
 * cost. The interval above is a period between starts, so the work already
 * done has to come out of it: sleeping the full interval after a 251 ms encode
 * would put the next frame 751 ms later, which is 1.3 fps and outside the band
 * this is supposed to hold.
 */
export function nextDelayMs(lastFrameCostMs: number): number {
	return Math.max(0, nextIntervalMs(lastFrameCostMs) - lastFrameCostMs);
}

/**
 * The opening control message. Lives here rather than in the panel because the
 * params are a contract with the model's manifest, not a detail of the DOM:
 * every shipped realtime manifest marks the prompt required, and an open
 * without it is refused 4000 before a worker is assigned. The realtime path
 * is conditioned, not image-to-image: the worker runs text-to-image from a
 * fresh latent and constrains it on the drawing with a sketch T2I-Adapter, so
 * the scale it sends is the conditioning scale, `structure_strength`,
 * declared by every shipped realtime manifest. The other strength the
 * manifest still declares belongs to queued image-to-image jobs, where the
 * drawing is fed back in; this path ignores it. The params open the session;
 * changes land through updateParamsMessage. `frame_header` declares the 21
 * byte header this client writes and reads back, and the API uses that header
 * only for a socket whose open control declared it here.
 */
export function openMessage(
	modelId: string,
	prompt: string,
	params: { structure_strength: number; steps: number }
): string {
	return JSON.stringify({
		type: 'open',
		frame_header: 2,
		model_id: modelId,
		params: {
			prompt: prompt.trim(),
			structure_strength: params.structure_strength,
			steps: params.steps
		}
	});
}

/**
 * A lasso selection, as the `mask` param carries it: one or more polygons of
 * x and y normalised to 0..1 of the frame. The worker renders the new frame
 * inside the polygons and keeps the previous frame outside them; `mask: null`
 * clears the selection. Only a manifest declaring `parameters.properties.mask`
 * (sdxl-turbo, vega-rt) understands it, which is why the panel sends it only
 * for a model that declares it.
 *
 * `prompt` is the selection's own edit prompt: the text the worker conditions
 * with on the frames that composite through the polygons, in place of the
 * session prompt. It is absent when no edit prompt applies, and a worker
 * without it in the manifest never receives one.
 */
export type RealtimeCanvasMask = { polygons: [number, number][][]; prompt?: string };

/** Every value the update control may carry, mask included. */
export type RealtimeCanvasParamValue = string | number | RealtimeCanvasMask | null;

/**
 * The update control message carrying a subset of the session's params.
 * Lives here beside openMessage for the same reason: the params are a
 * contract with the model's manifest. The prompt is trimmed exactly as
 * openMessage trims it, so the two cannot disagree about whitespace, and a
 * mask passes through as it arrived: it is already normalised by the caller.
 */
export function updateParamsMessage(params: Record<string, RealtimeCanvasParamValue>): string {
	const update = { ...params };
	if (typeof update.prompt === 'string') update.prompt = update.prompt.trim();
	return JSON.stringify({ type: 'update_params', params: update });
}

/**
 * The states issue #3 asks the panel to expose. The API's `queued` control
 * sets `queued` while the session waits for a free worker slot; `ready` or
 * `resumed` move it back out.
 */
export type ConnectionState =
	'idle' | 'connecting' | 'queued' | 'active' | 'resuming' | 'interrupted' | 'failed';

/**
 * The state a close code leaves the session in. The API's terminal close
 * codes (docs/connection-handling.md) are 4000 protocol violation, 4002
 * unsupported version, 4003 no worker capacity, 4004 unknown model, 4401
 * authentication no longer valid, and 4403 not permitted to open a realtime
 * session. A terminal close is failed because retrying the same open would
 * end the same way; anything else is interrupted, which keeps the canvas and
 * invites a reconnect. The whole 4000-4999 range fails, not only the codes
 * listed: a refusal this client has not been told about is still a refusal,
 * and a reconnect would meet the same close again.
 */
export function stateForCloseCode(code: number): ConnectionState {
	return code >= 4000 && code <= 4999 ? 'failed' : 'interrupted';
}

/**
 * What `ready` says this session may send. The API sends it from issue #617;
 * an older API omits it and then nothing here is self-checked, exactly as
 * before it shipped.
 */
export interface RealtimeCanvasLimits {
	max_frame_bytes: number;
	formats?: string[];
	width?: number;
	height?: number;
}

/**
 * Whether one encoded canvas image fits the session's byte cap.
 *
 * The cap is on the payload, which is what the API measures against its own
 * MAX_CANVAS_PAYLOAD_BYTES after the header this module adds. null means the
 * API sent no limits, so the answer is yes: the API's own drop and its 4005
 * refusal stay the only guard, as they were before limits existed.
 */
export function frameFitsLimits(
	payloadBytes: number,
	limits: RealtimeCanvasLimits | null
): boolean {
	return limits === null || payloadBytes <= limits.max_frame_bytes;
}

/**
 * The limits a ready carried, or null when it carried none or something
 * unusable. A missing or malformed value is an older API rather than a
 * protocol error: the session still runs and falls back to sending every
 * frame, which is what it did before limits existed.
 */
export function readyLimits(raw: unknown): RealtimeCanvasLimits | null {
	if (typeof raw !== 'object' || raw === null) return null;
	const max = (raw as { max_frame_bytes?: unknown }).max_frame_bytes;
	if (typeof max !== 'number' || !Number.isFinite(max) || max <= 0) return null;
	return raw as RealtimeCanvasLimits;
}

const OPEN = 1;
const UUID_RE = /^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$/i;

interface CanvasSocket {
	readyState: number;
	bufferedAmount: number;
	binaryType: BinaryType;
	send(data: string | ArrayBuffer | ArrayBufferView): void;
	close(code?: number): void;
	onopen: ((event: Event) => void) | null;
	onmessage: ((event: MessageEvent<string | ArrayBuffer>) => void) | null;
	onerror: ((event: Event) => void) | null;
	onclose: ((event: CloseEvent) => void) | null;
}

type DecodedFrame = { close(): void };

interface SessionGeneration {
	readonly socket: CanvasSocket;
	readonly modelId: string;
	readonly prompt: string;
	readonly params: RealtimeCanvasParams;
	sessionId: string | null;
	limits: RealtimeCanvasLimits | null;
	state: ConnectionState;
	changed: boolean;
	encoding: boolean;
	decoding: boolean;
	pendingFrame: GeneratedFrame | null;
	/** Counts this session's canvas frames from the first one it sends. */
	inputRevision: number;
	/** The revision of the newest frame actually drawn to the output. */
	shownRevision: number;
	timer: ReturnType<typeof setTimeout> | null;
	/** Idle milliseconds already waited, summed from the armed intervals. */
	idleMs: number;
	lastFrameCostMs: number;
	userClosing: boolean;
}

export interface RealtimeCanvasParams {
	structure_strength: number;
	steps: number;
}

export type RealtimeCanvasNotice =
	| ''
	| 'encode_failed'
	| 'decode_failed'
	| 'frame_too_large'
	| 'socket_error'
	| 'refused_protocol'
	| 'refused_version'
	| 'refused_capacity'
	| 'refused_model'
	| 'refused_forbidden'
	| 'session_revoked';

/**
 * Whether a notice is terminal: the session or the account refused the open,
 * so retrying on this page would end the same way. A revoked session recovers
 * only by signing in again, which reloads the studio, and a role change cannot
 * happen within the page, so the panel keeps Connect disabled until then.
 * Every other notice clears on a retry: capacity and a vanished model are
 * momentary, and a socket error or an encode failure says nothing about the
 * next attempt.
 */
export function isTerminalNotice(notice: RealtimeCanvasNotice): boolean {
	return notice === 'session_revoked' || notice === 'refused_forbidden';
}

/**
 * How far frames have travelled through one session, stage by stage.
 *
 * There is no `accepted` counter: the server says nothing when a frame lands,
 * so the last thing this client can observe is the frame leaving the socket.
 * `attempted` is a tick that started an encode, `replaced` a change folded
 * into the capture after it, `encoded` an encode that resolved, `sent` a frame
 * on the wire, `generated` a frame that arrived back for this session, and
 * `presented` one actually drawn.
 */
export type FrameCounters = {
	attempted: number;
	replaced: number;
	encoded: number;
	sent: number;
	generated: number;
	presented: number;
};

export interface RealtimeCanvasSessionOptions {
	getDrawCanvas: () => HTMLCanvasElement | undefined;
	getOutputCanvas: () => HTMLCanvasElement | undefined;
	isCanvasBlank: () => boolean;
	onState: (state: ConnectionState) => void;
	onQueuePosition: (position: number) => void;
	onNotice: (notice: RealtimeCanvasNotice) => void;
	onCounters: (counters: FrameCounters) => void;
	onAppliedParams: (
		params: Partial<{ prompt: string; structure_strength: number; steps: number }>
	) => void;
	webSocketFactory?: () => CanvasSocket;
	encode?: (canvas: HTMLCanvasElement) => Promise<Uint8Array<ArrayBuffer>>;
	decode?: (image: Uint8Array<ArrayBuffer>) => Promise<DecodedFrame>;
	drawDecoded?: (bitmap: DecodedFrame, canvas: HTMLCanvasElement) => void;
	setTimeout?: typeof globalThis.setTimeout;
	clearTimeout?: typeof globalThis.clearTimeout;
}

export interface RealtimeCanvasSession {
	connect(input: { modelId: string; prompt: string; params: RealtimeCanvasParams }): void;
	disconnect(): void;
	updateParams(params: Record<string, RealtimeCanvasParamValue>): void;
	markChanged(): void;
	destroy(): void;
}

function encodeCanvas(canvas: HTMLCanvasElement): Promise<Uint8Array<ArrayBuffer>> {
	return new Promise((resolve, reject) => {
		canvas.toBlob((blob) => {
			if (!blob) {
				reject(new Error('the canvas produced no image'));
				return;
			}
			blob.arrayBuffer().then((buffer) => resolve(new Uint8Array(buffer)), reject);
		}, 'image/webp');
	});
}

async function decodeCanvas(image: Uint8Array<ArrayBuffer>): Promise<DecodedFrame> {
	return createImageBitmap(new Blob([image], { type: 'image/webp' }));
}

function drawDecodedFrame(bitmap: DecodedFrame, canvas: HTMLCanvasElement): void {
	const context = canvas.getContext('2d');
	if (!context) return;
	context.drawImage(bitmap as CanvasImageSource, 0, 0, 512, 512);
}

function defaultSocketFactory(): CanvasSocket {
	const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
	return new WebSocket(`${scheme}//${location.host}/api/v1/realtime`) as CanvasSocket;
}

function refusalNotice(code: number): RealtimeCanvasNotice {
	if (code === 4403) return 'refused_forbidden';
	if (code === 4401) return 'session_revoked';
	if (code === 4004) return 'refused_model';
	if (code === 4003) return 'refused_capacity';
	if (code === 4002) return 'refused_version';
	if (code === 4000) return 'refused_protocol';
	// 4005 refuses a frame, not the session: the API keeps the socket and
	// says only that this drawing will not fit on the wire.
	if (code === 4005) return 'frame_too_large';
	return 'socket_error';
}

export function createRealtimeCanvasSession(
	options: RealtimeCanvasSessionOptions
): RealtimeCanvasSession {
	const makeSocket = options.webSocketFactory ?? defaultSocketFactory;
	const encode = options.encode ?? encodeCanvas;
	const decode = options.decode ?? decodeCanvas;
	const draw = options.drawDecoded ?? drawDecodedFrame;
	const schedule = options.setTimeout ?? globalThis.setTimeout;
	const cancel = options.clearTimeout ?? globalThis.clearTimeout;
	let current: SessionGeneration | null = null;
	let attempted = 0;
	let replaced = 0;
	let encoded = 0;
	let sent = 0;
	let generated = 0;
	let presented = 0;
	let destroyed = false;
	let notice: RealtimeCanvasNotice = '';

	function isCurrent(generation: SessionGeneration): boolean {
		return !destroyed && current === generation;
	}

	/** Hand the caller a fresh snapshot, so a panel holding the last one can
	 * see which stage moved without comparing against a mutated object. */
	function publishCounters(): void {
		options.onCounters({ attempted, replaced, encoded, sent, generated, presented });
	}

	function setNotice(value: RealtimeCanvasNotice): void {
		notice = value;
		options.onNotice(value);
	}

	function stopTimer(generation: SessionGeneration): void {
		if (generation.timer !== null) cancel(generation.timer);
		generation.timer = null;
	}

	function armCapture(generation: SessionGeneration, delay: number): void {
		stopTimer(generation);
		generation.timer = schedule(() => void captureTick(generation), delay);
	}

	function retire(generation: SessionGeneration): void {
		stopTimer(generation);
		generation.pendingFrame = null;
		generation.userClosing = true;
		generation.socket.close(1000);
	}

	function setState(generation: SessionGeneration, state: ConnectionState): void {
		if (!isCurrent(generation)) return;
		generation.state = state;
		options.onState(state);
	}

	function sending(generation: SessionGeneration): boolean {
		return generation.state === 'active';
	}

	function canContinue(generation: SessionGeneration): boolean {
		return isCurrent(generation) && !generation.userClosing;
	}

	function sendUpdate(
		generation: SessionGeneration,
		params: Record<string, RealtimeCanvasParamValue>
	): void {
		if (
			!isCurrent(generation) ||
			generation.userClosing ||
			!generation.sessionId ||
			generation.socket.readyState !== OPEN ||
			(generation.state !== 'active' && generation.state !== 'resuming')
		)
			return;
		generation.socket.send(updateParamsMessage(params));
	}

	async function captureTick(generation: SessionGeneration): Promise<void> {
		generation.timer = null;
		const canvas = options.getDrawCanvas();
		if (
			!isCurrent(generation) ||
			generation.userClosing ||
			!generation.sessionId ||
			generation.socket.readyState !== OPEN ||
			!canvas ||
			!sending(generation)
		)
			return;

		if (
			!shouldSendFrame({
				changed: generation.changed,
				encoding: generation.encoding,
				buffered: generation.socket.bufferedAmount
			})
		) {
			const interval = nextIntervalMs(generation.lastFrameCostMs);
			// A tick held back by a pending change (backpressure, an encode in
			// flight) is not idle, and counting it would cut the next real pause short.
			if (!generation.changed) generation.idleMs += interval;
			if (shouldStopPolling(generation.idleMs, generation.changed)) return;
			armCapture(generation, interval);
			return;
		}

		const forSession = generation.sessionId;
		const started = performance.now();
		generation.changed = false;
		generation.encoding = true;
		attempted += 1;
		publishCounters();
		try {
			const image = await encode(canvas);
			// A reconnect resets the counters, and an encode it outlived must
			// not count toward the new session.
			if (canContinue(generation)) {
				encoded += 1;
				publishCounters();
			}
			if (canContinue(generation) && generation.socket.readyState === OPEN) {
				if (sending(generation)) {
					if (frameFitsLimits(image.length, generation.limits)) {
						// Counted only here: a frame the cap holds back never
						// reaches the wire, so it must not take a revision.
						generation.inputRevision += 1;
						generation.socket.send(canvasFrame(forSession, generation.inputRevision, image));
						if (notice === 'frame_too_large') setNotice('');
						sent += 1;
						publishCounters();
					} else {
						// The drawing did not change, so re-encoding it would
						// hit the same cap; the notice stands until a frame
						// fits, and the API would drop this one anyway (4005).
						setNotice('frame_too_large');
					}
				} else {
					generation.changed = true;
				}
			}
		} catch {
			if (canContinue(generation)) {
				generation.changed = true;
				setNotice('encode_failed');
			}
		} finally {
			generation.encoding = false;
			generation.lastFrameCostMs = performance.now() - started;
		}
		if (canContinue(generation)) armCapture(generation, nextDelayMs(generation.lastFrameCostMs));
	}

	async function drainGenerated(generation: SessionGeneration): Promise<void> {
		if (generation.decoding) return;
		generation.decoding = true;
		try {
			while (canContinue(generation) && generation.pendingFrame !== null) {
				const frame = generation.pendingFrame;
				generation.pendingFrame = null;
				// Arrival already dropped what was behind the shown revision,
				// but a newer frame can have been drawn while this one waited
				// its turn, so the guard runs again before the decode is paid.
				if (isStaleOutput(frame.revision, generation.shownRevision)) continue;
				try {
					const bitmap = await decode(frame.image);
					try {
						if (canContinue(generation)) {
							const canvas = options.getOutputCanvas();
							if (canvas) {
								draw(bitmap, canvas);
								// Only a drawn frame moves what is on screen.
								generation.shownRevision = frame.revision;
								presented += 1;
								publishCounters();
							}
						}
					} finally {
						bitmap.close();
					}
				} catch {
					if (canContinue(generation)) setNotice('decode_failed');
				}
			}
		} finally {
			generation.decoding = false;
		}
	}

	function handleControl(generation: SessionGeneration, text: string): void {
		let control: {
			type?: string;
			session_id?: string;
			position?: unknown;
			code?: number;
			params?: unknown;
			limits?: unknown;
		};
		try {
			control = JSON.parse(text) as typeof control;
		} catch {
			return;
		}
		if (!canContinue(generation)) return;
		if (control.type === 'ready') {
			if (!control.session_id || !UUID_RE.test(control.session_id)) {
				setNotice('socket_error');
				setState(generation, 'failed');
				stopTimer(generation);
				generation.pendingFrame = null;
				current = null;
				generation.socket.close(1000);
				return;
			}
			generation.sessionId = control.session_id;
			generation.limits = readyLimits(control.limits);
			setState(generation, 'active');
			setNotice('');
			generation.changed = !options.isCanvasBlank();
			armCapture(generation, FAST_INTERVAL_MS);
			return;
		}
		if (control.type === 'queued') {
			const position = control.position;
			if (typeof position === 'number' && Number.isInteger(position) && position >= 1) {
				setState(generation, 'queued');
				options.onQueuePosition(position);
			}
			return;
		}
		if (control.type === 'interrupted') {
			setState(generation, 'resuming');
			return;
		}
		if (control.type === 'resumed') {
			setState(generation, 'active');
			generation.changed = true;
			generation.idleMs = 0;
			armCapture(generation, FAST_INTERVAL_MS);
			return;
		}
		if (control.type === 'params_updated' && control.params) {
			const params = control.params as {
				prompt?: unknown;
				structure_strength?: unknown;
				steps?: unknown;
			};
			const applied: Partial<{ prompt: string; structure_strength: number; steps: number }> = {};
			if (typeof params.prompt === 'string') applied.prompt = params.prompt;
			if (typeof params.structure_strength === 'number') {
				applied.structure_strength = params.structure_strength;
			}
			if (typeof params.steps === 'number') applied.steps = params.steps;
			options.onAppliedParams(applied);
			generation.changed = true;
			generation.idleMs = 0;
			if (sending(generation)) armCapture(generation, FAST_INTERVAL_MS);
			return;
		}
		if (control.type === 'error') setNotice(refusalNotice(control.code ?? 0));
	}

	function attach(generation: SessionGeneration): void {
		const socket = generation.socket;
		socket.binaryType = 'arraybuffer';
		socket.onopen = () => {
			if (!canContinue(generation)) return;
			socket.send(
				openMessage(generation.modelId, generation.prompt, {
					structure_strength: generation.params.structure_strength,
					steps: generation.params.steps
				})
			);
			options.onAppliedParams({
				prompt: generation.prompt.trim(),
				structure_strength: generation.params.structure_strength,
				steps: generation.params.steps
			});
		};
		socket.onmessage = (event) => {
			if (!canContinue(generation)) return;
			if (typeof event.data === 'string') {
				handleControl(generation, event.data);
				return;
			}
			if (!generation.sessionId || !(event.data instanceof ArrayBuffer)) return;
			const frame = parseGeneratedFrame(new Uint8Array(event.data), generation.sessionId);
			if (frame === null || frame.image.length === 0) return;
			generated += 1;
			publishCounters();
			// Dropped on arrival, so an old frame neither costs a decode nor
			// occupies the slot a newer frame should take.
			if (isStaleOutput(frame.revision, generation.shownRevision)) return;
			generation.pendingFrame = frame;
			void drainGenerated(generation);
		};
		socket.onerror = () => {
			if (canContinue(generation)) setNotice('socket_error');
		};
		socket.onclose = (event) => {
			if (!isCurrent(generation)) return;
			stopTimer(generation);
			generation.sessionId = null;
			generation.pendingFrame = null;
			const state = generation.userClosing ? 'idle' : stateForCloseCode(event.code);
			generation.state = state;
			options.onState(state);
			current = null;
			if (generation.state === 'failed' && !notice) setNotice(refusalNotice(event.code));
		};
	}

	return {
		connect(input) {
			if (destroyed) return;
			if (current) retire(current);
			const socket = makeSocket();
			const generation: SessionGeneration = {
				socket,
				modelId: input.modelId,
				prompt: input.prompt,
				params: input.params,
				sessionId: null,
				limits: null,
				state: 'connecting',
				changed: false,
				encoding: false,
				decoding: false,
				pendingFrame: null,
				inputRevision: 0,
				shownRevision: 0,
				timer: null,
				idleMs: 0,
				lastFrameCostMs: 0,
				userClosing: false
			};
			current = generation;
			attempted = 0;
			replaced = 0;
			encoded = 0;
			sent = 0;
			generated = 0;
			presented = 0;
			publishCounters();
			setNotice('');
			options.onState('connecting');
			attach(generation);
		},
		disconnect() {
			if (!current) return;
			current.userClosing = true;
			stopTimer(current);
			current.socket.close(1000);
		},
		updateParams(params) {
			if (current) sendUpdate(current, params);
		},
		markChanged() {
			if (!current) return;
			// A change on top of one already waiting folds into the capture
			// after it; the latest canvas wins either way, but the fold is a
			// frame the loop chose not to send twice.
			// Counted without publishing: this runs on every pointer move, and
			// the next capture publishes the total anyway.
			if (current.changed) replaced += 1;
			current.changed = true;
			current.idleMs = 0;
			// Nothing is armed and no encode is running, so the loop has
			// stopped: waiting out an interval first would show the person a
			// stroke that lags behind them, and there is no backlog to clear.
			if (sending(current) && current.timer === null && !current.encoding) {
				armCapture(current, 0);
			}
		},
		destroy() {
			if (destroyed) return;
			destroyed = true;
			if (current) {
				stopTimer(current);
				current.pendingFrame = null;
				current.socket.close(1000);
				current = null;
			}
		}
	};
}
