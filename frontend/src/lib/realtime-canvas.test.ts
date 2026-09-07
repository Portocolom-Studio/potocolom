// node --test with the built in type stripping, so the canvas wire keeps a
// check without pulling a test framework into the frontend (see Makefile
// verify-frontend). These assert the framing byte for byte and the send
// policy, because a frame the API refuses looks identical to a working one
// from inside the panel.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	CANVAS_FRAME,
	FAST_INTERVAL_MS,
	FRAME_HEADER_BYTES,
	GENERATED_FRAME,
	IDLE_TICKS_BEFORE_STOP,
	SLOW_INTERVAL_MS,
	canvasFrame,
	nextDelayMs,
	nextIntervalMs,
	openMessage,
	parseGeneratedFrame,
	shouldSendFrame,
	stateForCloseCode,
	updateParamsMessage,
	uuidBytes,
	createRealtimeCanvasSession,
	type ConnectionState,
	type RealtimeCanvasSession
} from './realtime-canvas.ts';

const SESSION = '3f2504e0-4f89-11d3-9a0c-0305e82c3301';
/** The same session with only the final byte changed. */
const NEARLY = '3f2504e0-4f89-11d3-9a0c-0305e82c3302';
/** Only the first byte changed. */
const NEARLY_FIRST = '402504e0-4f89-11d3-9a0c-0305e82c3301';
/** Only a middle byte changed. */
const NEARLY_MIDDLE = '3f2504e0-4f89-21d3-9a0c-0305e82c3301';
/**
 * The three near-miss fixtures differ from SESSION at the first byte, a middle
 * byte and the last byte respectively. All three are needed: a comparison
 * weakened to check only some of those positions passes as long as every
 * fixture differs at a checked one, so a fixture at each end of the id cannot
 * pin the bytes between them.
 */

test('a uuid becomes its sixteen raw bytes', () => {
	const bytes = uuidBytes(SESSION);
	assert.equal(bytes.length, 16);
	assert.equal(bytes[0], 0x3f);
	assert.equal(bytes[15], 0x01);
});

test('a value that is not a uuid is refused rather than framed', () => {
	assert.throws(() => uuidBytes('not-a-uuid'));
	assert.throws(() => uuidBytes(''));
	// One hex digit short: a truncated id would otherwise frame a short header.
	assert.throws(() => uuidBytes('3f2504e0-4f89-11d3-9a0c-0305e82c330'));
});

test('a canvas frame carries the kind byte, the whole session id, then the image', () => {
	const image = new Uint8Array([0x52, 0x49, 0x46, 0x46, 0x99]);
	const frame = canvasFrame(SESSION, image);

	assert.equal(frame.length, FRAME_HEADER_BYTES + image.length);
	assert.equal(frame[0], CANVAS_FRAME);
	// Every header byte, so a partial write cannot pass.
	assert.deepEqual(frame.subarray(1, FRAME_HEADER_BYTES), uuidBytes(SESSION));
	assert.deepEqual(frame.subarray(FRAME_HEADER_BYTES), image);
});

test('a generated frame yields the image bytes after the header', () => {
	const image = new Uint8Array([1, 2, 3, 4]);
	const frame = new Uint8Array(FRAME_HEADER_BYTES + image.length);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(SESSION), 1);
	frame.set(image, FRAME_HEADER_BYTES);

	assert.deepEqual(parseGeneratedFrame(frame, SESSION), image);
});

test('a frame for a session differing in one byte is not ours', () => {
	for (const other of [NEARLY, NEARLY_FIRST, NEARLY_MIDDLE]) {
		const frame = new Uint8Array(FRAME_HEADER_BYTES + 2);
		frame[0] = GENERATED_FRAME;
		frame.set(uuidBytes(other), 1);

		assert.equal(parseGeneratedFrame(frame, SESSION), null, other);
	}
});

test('a canvas frame echoed back is not treated as generated output', () => {
	const frame = canvasFrame(SESSION, new Uint8Array([7]));
	assert.equal(parseGeneratedFrame(frame, SESSION), null);
});

test('a frame shorter than the header is refused', () => {
	const short = new Uint8Array(FRAME_HEADER_BYTES - 1);
	short[0] = GENERATED_FRAME;
	short.set(uuidBytes(SESSION).subarray(0, 15), 1);

	assert.equal(parseGeneratedFrame(short, SESSION), null);
});

test('a header with no image is empty rather than null', () => {
	// The worker should not send one, but an empty payload is still our frame:
	// reporting null would make it indistinguishable from another session's.
	const frame = new Uint8Array(FRAME_HEADER_BYTES);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(SESSION), 1);

	assert.deepEqual(parseGeneratedFrame(frame, SESSION), new Uint8Array(0));
});

test('a frame is sent only when there is a change, no encode, and an empty socket', () => {
	assert.ok(shouldSendFrame({ changed: true, encoding: false, buffered: 0 }));
	// Nothing new to send.
	assert.ok(!shouldSendFrame({ changed: false, encoding: false, buffered: 0 }));
	// One encode in flight.
	assert.ok(!shouldSendFrame({ changed: true, encoding: true, buffered: 0 }));
	// Anything still queued on the socket: the backlog would only grow.
	assert.ok(!shouldSendFrame({ changed: true, encoding: false, buffered: 1 }));
});

test('the cadence stays inside the two to four fps band', () => {
	assert.equal(nextIntervalMs(10), FAST_INTERVAL_MS);
	assert.equal(nextIntervalMs(FAST_INTERVAL_MS), FAST_INTERVAL_MS);
	assert.equal(nextIntervalMs(FAST_INTERVAL_MS + 1), SLOW_INTERVAL_MS);
	assert.equal(nextIntervalMs(5_000), SLOW_INTERVAL_MS);
	assert.ok(SLOW_INTERVAL_MS <= 500 && FAST_INTERVAL_MS >= 250);
});

test('the delay is what is left of the period, not the whole period', () => {
	// Waiting the full interval after the work would put a 251 ms encode 751 ms
	// from the previous start, which is 1.3 fps and outside the band above.
	for (const cost of [0, 10, 120, 250, 251, 400, 499]) {
		const period = cost + nextDelayMs(cost);
		assert.ok(period >= 250 && period <= 500, `cost ${cost} gave a ${period} ms period`);
	}
});

test('a frame that outruns the slow interval schedules immediately', () => {
	// Nothing negative, and no attempt to catch up by queueing work.
	assert.equal(nextDelayMs(SLOW_INTERVAL_MS), 0);
	assert.equal(nextDelayMs(5_000), 0);
});

test('the open message carries the prompt every realtime manifest requires', () => {
	// The regression this whole module exists to prevent: an open with no params
	// is refused 4000 by the API before a worker is assigned.
	const message = JSON.parse(
		openMessage('vega-rt', '  a red house  ', { structure_strength: 0.5, steps: 10 })
	);
	assert.equal(message.type, 'open');
	assert.equal(message.model_id, 'vega-rt');
	assert.deepEqual(message.params, {
		prompt: 'a red house',
		structure_strength: 0.5,
		steps: 10
	});
});

test('the open message carries the structure strength and steps passed to it', () => {
	const message = JSON.parse(
		openMessage('vega-rt', 'a cat', { structure_strength: 0.25, steps: 30 })
	);
	assert.equal(message.params.structure_strength, 0.25);
	assert.equal(message.params.steps, 30);
});

test('the update message carries a subset of the session params', () => {
	const message = JSON.parse(updateParamsMessage({ structure_strength: 0.3 }));
	assert.equal(message.type, 'update_params');
	assert.deepEqual(message.params, { structure_strength: 0.3 });
});

test('the update message trims the prompt exactly like the open message', () => {
	const message = JSON.parse(updateParamsMessage({ prompt: '  a blue house  ', steps: 6 }));
	assert.equal(message.params.prompt, 'a blue house');
	assert.equal(message.params.steps, 6);
});

test('a refusal fails the session and anything else invites a reconnect', () => {
	for (const code of [4000, 4002, 4003, 4004]) {
		assert.equal(stateForCloseCode(code), 'failed', `code ${code}`);
	}
	// A normal close, or a dropped connection, keeps the canvas recoverable.
	assert.equal(stateForCloseCode(1000), 'interrupted');
	assert.equal(stateForCloseCode(1006), 'interrupted');
});

class TestSocket {
	readyState = 0;
	emitClose = true;
	bufferedAmount = 0;
	binaryType: BinaryType = 'arraybuffer';
	sent: Array<string | ArrayBuffer | ArrayBufferView> = [];
	onopen: ((event: Event) => void) | null = null;
	onmessage: ((event: MessageEvent<string | ArrayBuffer>) => void) | null = null;
	onerror: ((event: Event) => void) | null = null;
	onclose: ((event: CloseEvent) => void) | null = null;

	send(data: string | ArrayBuffer | ArrayBufferView): void {
		this.sent.push(data);
	}

	open(): void {
		this.readyState = 1;
		this.onopen?.(new Event('open'));
	}

	message(data: string | ArrayBuffer): void {
		this.onmessage?.({ data } as MessageEvent<string | ArrayBuffer>);
	}

	close(code = 1000): void {
		this.readyState = 3;
		if (this.emitClose) this.onclose?.({ code } as CloseEvent);
	}
}

function sessionHarness(
	options: {
		encode?: (canvas: HTMLCanvasElement) => Promise<Uint8Array<ArrayBuffer>>;
		decode?: (image: Uint8Array<ArrayBuffer>) => Promise<{ close(): void }>;
		isCanvasBlank?: () => boolean;
	} = {}
): {
	session: RealtimeCanvasSession;
	sockets: TestSocket[];
	states: ConnectionState[];
	notices: string[];
	applied: Array<Record<string, unknown>>;
	counters: Array<[number, number]>;
	draws: number[];
	tick(): void;
	timerCount(): number;
} {
	const sockets: TestSocket[] = [];
	const timers = new Map<number, () => void>();
	let nextTimer = 0;
	const schedule = ((callback: () => void) => {
		const id = ++nextTimer;
		timers.set(id, callback);
		return id;
	}) as typeof globalThis.setTimeout;
	const cancel = ((id: ReturnType<typeof setTimeout>) => {
		timers.delete(id as unknown as number);
	}) as typeof globalThis.clearTimeout;
	const states: ConnectionState[] = [];
	const notices: string[] = [];
	const applied: Array<Record<string, unknown>> = [];
	const counters: Array<[number, number]> = [];
	const draws: number[] = [];
	const session = createRealtimeCanvasSession({
		getDrawCanvas: () => ({}) as HTMLCanvasElement,
		getOutputCanvas: () => ({}) as HTMLCanvasElement,
		isCanvasBlank: options.isCanvasBlank ?? (() => false),
		onState: (state) => states.push(state),
		onNotice: (notice) => notices.push(notice),
		onCounters: (sent, rendered) => counters.push([sent, rendered]),
		onAppliedParams: (params) => applied.push(params),
		webSocketFactory: () => {
			const socket = new TestSocket();
			sockets.push(socket);
			return socket;
		},
		encode: options.encode ?? (async () => new Uint8Array([1, 2, 3])),
		decode: options.decode ?? (async () => ({ close() {} })),
		drawDecoded: () => draws.push(1),
		setTimeout: schedule,
		clearTimeout: cancel
	});
	return {
		session,
		sockets,
		states,
		notices,
		applied,
		counters,
		draws,
		tick() {
			const entry = timers.entries().next().value as [number, () => void] | undefined;
			if (!entry) throw new Error('no timer');
			timers.delete(entry[0]);
			entry[1]();
		},
		timerCount: () => timers.size
	};
}

function ready(socket: TestSocket, id = SESSION): void {
	socket.open();
	socket.message(JSON.stringify({ type: 'ready', session_id: id }));
}

function generated(id = SESSION): ArrayBuffer {
	const frame = new Uint8Array(FRAME_HEADER_BYTES + 2);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(id), 1);
	frame.set([9, 8], FRAME_HEADER_BYTES);
	return frame.buffer;
}

function emptyGenerated(id = SESSION): ArrayBuffer {
	const frame = new Uint8Array(FRAME_HEADER_BYTES);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(id), 1);
	return frame.buffer;
}

test('an interruption during encode keeps the canvas pending for resume', async () => {
	let resolveEncode!: (image: Uint8Array<ArrayBuffer>) => void;
	const harness = sessionHarness({
		encode: () =>
			new Promise((resolve) => {
				resolveEncode = resolve;
			})
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	harness.tick();
	harness.session.markChanged();
	assert.equal(harness.timerCount(), 0);
	harness.sockets[0].message(JSON.stringify({ type: 'interrupted' }));
	resolveEncode(new Uint8Array([4]));
	await Promise.resolve();
	assert.equal(harness.sockets[0].sent.filter((data) => typeof data !== 'string').length, 0);
	assert.equal(harness.timerCount(), 1);
	harness.sockets[0].message(JSON.stringify({ type: 'resumed' }));
	assert.equal(harness.timerCount(), 1);
});

test('resume resend encodes and sends the complete current frame', async () => {
	let encodes = 0;
	const harness = sessionHarness({
		encode: async () => {
			encodes += 1;
			return new Uint8Array([encodes]);
		}
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	harness.sockets[0].message(JSON.stringify({ type: 'interrupted' }));
	harness.sockets[0].message(JSON.stringify({ type: 'resumed' }));
	harness.tick();
	await Promise.resolve();
	const frames = harness.sockets[0].sent.filter((data) => typeof data !== 'string');
	assert.equal(frames.length, 1);
	assert.deepEqual(
		new Uint8Array(frames[0] as ArrayBuffer).subarray(FRAME_HEADER_BYTES),
		new Uint8Array([1])
	);
});

test('late socket, encode, and decode work cannot affect a replacement session', async () => {
	let resolveEncode!: (image: Uint8Array<ArrayBuffer>) => void;
	let resolveDecode!: (bitmap: { close(): void }) => void;
	const harness = sessionHarness({
		encode: () =>
			new Promise((resolve) => {
				resolveEncode = resolve;
			}),
		decode: () =>
			new Promise((resolve) => {
				resolveDecode = resolve;
			})
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'old',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const oldSocket = harness.sockets[0];
	ready(oldSocket);
	harness.tick();
	oldSocket.message(generated());
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'new',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const newSocket = harness.sockets[1];
	oldSocket.close(1006);
	oldSocket.message(JSON.stringify({ type: 'ready', session_id: SESSION }));
	resolveEncode(new Uint8Array([7]));
	resolveDecode({ close() {} });
	await Promise.resolve();
	assert.deepEqual(harness.states.slice(-1), ['connecting']);
	assert.equal(harness.draws.length, 0);
	ready(newSocket, NEARLY);
	assert.equal(harness.states.at(-1), 'active');
});

test('an invalid ready id fails and closes without leaving a capture timer', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	harness.sockets[0].open();
	harness.sockets[0].message(JSON.stringify({ type: 'ready', session_id: 'not-a-uuid' }));
	assert.equal(harness.states.at(-1), 'failed');
	assert.equal(harness.sockets[0].readyState, 3);
	assert.equal(harness.timerCount(), 0);
});

test('encode and decode failures notify while preserving the session', async () => {
	const harness = sessionHarness({
		encode: async () => {
			throw new Error('encode');
		},
		decode: async () => {
			throw new Error('decode');
		}
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	harness.tick();
	await Promise.resolve();
	assert.equal(harness.notices.at(-1), 'encode_failed');
	harness.sockets[0].message(generated());
	await Promise.resolve();
	assert.equal(harness.notices.at(-1), 'decode_failed');
	assert.equal(harness.states.at(-1), 'active');
});

test('params acknowledgement updates controls and wakes capture', () => {
	const harness = sessionHarness({ isCanvasBlank: () => true });
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	for (let tick = 0; tick < IDLE_TICKS_BEFORE_STOP; tick += 1) harness.tick();
	assert.equal(harness.timerCount(), 0);
	harness.sockets[0].message(
		JSON.stringify({
			type: 'params_updated',
			params: { prompt: 'a dog', structure_strength: 0.7, steps: 12 }
		})
	);
	assert.deepEqual(harness.applied.at(-1), { prompt: 'a dog', structure_strength: 0.7, steps: 12 });
	assert.equal(harness.timerCount(), 1);
	harness.tick();
	return Promise.resolve().then(() => {
		const frames = harness.sockets[0].sent.filter((data) => typeof data !== 'string');
		assert.equal(frames.length, 1);
	});
});

test('destroy cancels capture timers and stale async completion cannot re-arm one', async () => {
	let resolveEncode!: (image: Uint8Array<ArrayBuffer>) => void;
	const harness = sessionHarness({
		encode: () =>
			new Promise((resolve) => {
				resolveEncode = resolve;
			})
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	harness.tick();
	harness.session.destroy();
	assert.equal(harness.timerCount(), 0);
	resolveEncode(new Uint8Array([2]));
	await Promise.resolve();
	assert.equal(harness.timerCount(), 0);
});

test('disconnect fences pending encode and decode work before close arrives', async () => {
	let resolveEncode!: (image: Uint8Array<ArrayBuffer>) => void;
	let resolveDecode!: (bitmap: { close(): void }) => void;
	const harness = sessionHarness({
		encode: () =>
			new Promise((resolve) => {
				resolveEncode = resolve;
			}),
		decode: () =>
			new Promise((resolve) => {
				resolveDecode = resolve;
			})
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	ready(socket);
	harness.tick();
	socket.emitClose = false;
	harness.session.disconnect();
	resolveEncode(new Uint8Array([5]));
	await Promise.resolve();
	assert.equal(harness.timerCount(), 0);
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const replacement = harness.sockets[1];
	ready(replacement);
	replacement.message(generated());
	replacement.emitClose = false;
	harness.session.disconnect();
	resolveDecode({ close() {} });
	await Promise.resolve();
	assert.equal(harness.draws.length, 0);
});

test('binary frames before ready and empty generated frames are ignored', async () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	socket.open();
	socket.message(generated());
	socket.message(JSON.stringify({ type: 'ready', session_id: SESSION }));
	socket.message(emptyGenerated());
	await Promise.resolve();
	assert.equal(harness.draws.length, 0);
	assert.equal(harness.counters.at(-1)?.[1], 0);
});
