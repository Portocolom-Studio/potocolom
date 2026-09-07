// Wire framing and send policy for the realtime drawing canvas (issue #3).
// Kept apart from the panel so node --test can exercise framing and the live
// session lifecycle without loading Svelte. The panel owns the DOM and controls.
//
// The wire is docs/connection-handling.md: a 17 byte header of one kind byte
// and the 16 byte session UUID, then a complete WebP image. That header
// carries no sequence number, so generated frames arrive in transport order
// and monotonic revisions remain issue #19's to add.

/** Browser to worker. */
export const CANVAS_FRAME = 0x01;
/** Worker to browser. */
export const GENERATED_FRAME = 0x02;
export const FRAME_HEADER_BYTES = 17;

/** Issue #3 asks for 2 to 4 fps. Finer adaptation is issue #42's. */
export const FAST_INTERVAL_MS = 250;
export const SLOW_INTERVAL_MS = 500;

/** Ticks with nothing to send before the capture loop stops arming itself. */
export const IDLE_TICKS_BEFORE_STOP = 8;

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
 * The buffer is named in the type because send and the Blob constructor both
 * reject the ArrayBufferLike a bare Uint8Array widens to.
 */
export function canvasFrame(sessionId: string, image: Uint8Array): Uint8Array<ArrayBuffer> {
	const frame = new Uint8Array(FRAME_HEADER_BYTES + image.length);
	frame[0] = CANVAS_FRAME;
	frame.set(uuidBytes(sessionId), 1);
	frame.set(image, FRAME_HEADER_BYTES);
	return frame;
}

/**
 * The image bytes of a generated frame, or null when the frame is not one:
 * too short, the wrong kind, or another session's. The whole UUID is compared
 * because a frame from a session that differs in one byte is still not ours.
 */
export function parseGeneratedFrame(
	data: Uint8Array<ArrayBuffer>,
	sessionId: string
): Uint8Array<ArrayBuffer> | null {
	if (data.length < FRAME_HEADER_BYTES) return null;
	if (data[0] !== GENERATED_FRAME) return null;
	const expected = uuidBytes(sessionId);
	for (let index = 0; index < 16; index += 1) {
		if (data[index + 1] !== expected[index]) return null;
	}
	return data.subarray(FRAME_HEADER_BYTES);
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
 * The period to aim for between frame starts. Backs off to the slow end of the
 * band when encoding and queueing a frame already costs more than the fast
 * interval. This measures the browser's own cost, not the model's: the
 * generated frame is not correlated to the canvas frame that produced it on
 * this wire, so a true round trip is not observable until issue #19 adds
 * revisions.
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
 * changes land through updateParamsMessage.
 */
export function openMessage(
	modelId: string,
	prompt: string,
	params: { structure_strength: number; steps: number }
): string {
	return JSON.stringify({
		type: 'open',
		model_id: modelId,
		params: {
			prompt: prompt.trim(),
			structure_strength: params.structure_strength,
			steps: params.steps
		}
	});
}

/**
 * The update control message carrying a subset of the session's params.
 * Lives here beside openMessage for the same reason: the params are a
 * contract with the model's manifest. The prompt is trimmed exactly as
 * openMessage trims it, so the two cannot disagree about whitespace.
 */
export function updateParamsMessage(params: Record<string, string | number>): string {
	const update = { ...params };
	if (typeof update.prompt === 'string') update.prompt = update.prompt.trim();
	return JSON.stringify({ type: 'update_params', params: update });
}

/**
 * The states issue #3 asks the panel to expose. Nothing sets `queued` yet:
 * the shipped 4003 is an immediate full-pool refusal, and the admission queue
 * that would report a queued state is issue #19's (see the shipped-status note
 * in docs/connection-handling.md). It stays named here so the wire source is
 * the only piece missing when that lands.
 */
export type ConnectionState =
	'idle' | 'connecting' | 'queued' | 'active' | 'resuming' | 'interrupted' | 'failed';

/**
 * The state a close code leaves the session in. The API's refusal codes are
 * docs/api.md: 4000 protocol violation, 4002 unsupported version, 4003 no
 * worker capacity, 4004 unknown model. A refusal is failed because retrying
 * the same open would be refused the same way; anything else is interrupted,
 * which keeps the canvas and invites a reconnect.
 */
export function stateForCloseCode(code: number): ConnectionState {
	return code >= 4000 && code <= 4004 ? 'failed' : 'interrupted';
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
	state: ConnectionState;
	changed: boolean;
	encoding: boolean;
	decoding: boolean;
	pendingFrame: Uint8Array<ArrayBuffer> | null;
	timer: ReturnType<typeof setTimeout> | null;
	idleTicks: number;
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
	| 'socket_error'
	| 'refused_protocol'
	| 'refused_version'
	| 'refused_capacity'
	| 'refused_model';

export interface RealtimeCanvasSessionOptions {
	getDrawCanvas: () => HTMLCanvasElement | undefined;
	getOutputCanvas: () => HTMLCanvasElement | undefined;
	isCanvasBlank: () => boolean;
	onState: (state: ConnectionState) => void;
	onNotice: (notice: RealtimeCanvasNotice) => void;
	onCounters: (sent: number, rendered: number) => void;
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
	updateParams(params: Record<string, string | number>): void;
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
	if (code === 4004) return 'refused_model';
	if (code === 4003) return 'refused_capacity';
	if (code === 4002) return 'refused_version';
	if (code === 4000) return 'refused_protocol';
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
	let sent = 0;
	let rendered = 0;
	let destroyed = false;
	let notice: RealtimeCanvasNotice = '';

	function isCurrent(generation: SessionGeneration): boolean {
		return !destroyed && current === generation;
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
		params: Record<string, string | number>
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
			generation.idleTicks += 1;
			if (generation.idleTicks >= IDLE_TICKS_BEFORE_STOP && !generation.changed) return;
			armCapture(generation, nextDelayMs(generation.lastFrameCostMs));
			return;
		}

		const forSession = generation.sessionId;
		const started = performance.now();
		generation.changed = false;
		generation.encoding = true;
		try {
			const image = await encode(canvas);
			if (canContinue(generation) && generation.socket.readyState === OPEN) {
				if (sending(generation)) {
					generation.socket.send(canvasFrame(forSession, image));
					sent += 1;
					options.onCounters(sent, rendered);
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
				const image = generation.pendingFrame;
				generation.pendingFrame = null;
				try {
					const bitmap = await decode(image);
					try {
						if (canContinue(generation)) {
							const canvas = options.getOutputCanvas();
							if (canvas) {
								draw(bitmap, canvas);
								rendered += 1;
								options.onCounters(sent, rendered);
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
			code?: number;
			params?: unknown;
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
			setState(generation, 'active');
			setNotice('');
			generation.changed = !options.isCanvasBlank();
			armCapture(generation, FAST_INTERVAL_MS);
			return;
		}
		if (control.type === 'interrupted') {
			setState(generation, 'resuming');
			return;
		}
		if (control.type === 'resumed') {
			setState(generation, 'active');
			generation.changed = true;
			generation.idleTicks = 0;
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
			generation.idleTicks = 0;
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
			const image = parseGeneratedFrame(new Uint8Array(event.data), generation.sessionId);
			if (image === null || image.length === 0) return;
			generation.pendingFrame = image;
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
				state: 'connecting',
				changed: false,
				encoding: false,
				decoding: false,
				pendingFrame: null,
				timer: null,
				idleTicks: 0,
				lastFrameCostMs: 0,
				userClosing: false
			};
			current = generation;
			sent = 0;
			rendered = 0;
			options.onCounters(sent, rendered);
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
			current.changed = true;
			current.idleTicks = 0;
			if (sending(current) && current.timer === null && !current.encoding) {
				armCapture(current, FAST_INTERVAL_MS);
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
