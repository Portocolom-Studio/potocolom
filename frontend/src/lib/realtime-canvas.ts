// Wire framing and send policy for the realtime drawing canvas (issue #3).
// Kept apart from the panel so the rules the canvas depends on are reachable
// from node --test: this module holds the framing, the send predicate, the
// cadence and the opening message. The panel owns the DOM and the session
// lifecycle.
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
 * without it is refused 4000 before a worker is assigned. Strength and steps
 * are declared by every shipped realtime manifest too, so sending them is
 * within the contract. A previous version of this panel was built against a
 * permissive fake and never connected.
 */
export function openMessage(
	modelId: string,
	prompt: string,
	params: { strength: number; steps: number }
): string {
	return JSON.stringify({
		type: 'open',
		model_id: modelId,
		params: { prompt: prompt.trim(), strength: params.strength, steps: params.steps }
	});
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
