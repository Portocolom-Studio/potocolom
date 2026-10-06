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
	IDLE_STOP_MS,
	SLOW_INTERVAL_MS,
	canvasFrame,
	frameFitsLimits,
	isStaleOutput,
	nextDelayMs,
	nextIntervalMs,
	openMessage,
	parseGeneratedFrame,
	readyLimits,
	shouldSendFrame,
	shouldStopPolling,
	stateForCloseCode,
	updateParamsMessage,
	uuidBytes,
	createRealtimeCanvasSession,
	isTerminalNotice,
	type ConnectionState,
	type FrameCounters,
	type RealtimeCanvasLimits,
	type RealtimeCanvasMask,
	type RealtimeCanvasNotice,
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

test('a canvas frame carries the kind byte, the whole session id, the revision and the image', () => {
	const image = new Uint8Array([0x52, 0x49, 0x46, 0x46, 0x99]);
	const frame = canvasFrame(SESSION, 70000, image);

	assert.equal(frame.length, FRAME_HEADER_BYTES + image.length);
	assert.equal(frame[0], CANVAS_FRAME);
	// Every header byte up to the revision, so a partial write cannot pass.
	assert.deepEqual(frame.subarray(1, 17), uuidBytes(SESSION));
	// 70000 is 0x00011170: four bytes, big endian. A 16 bit write would have
	// lost the 0x0001 and a little endian one reads 0x70110100.
	assert.deepEqual([...frame.subarray(17, 21)], [0x00, 0x01, 0x11, 0x70]);
	assert.deepEqual(frame.subarray(FRAME_HEADER_BYTES), image);
});

test('a generated frame round trips its revision and image back to the caller', () => {
	const image = new Uint8Array([1, 2, 3, 4]);
	const frame = new Uint8Array(FRAME_HEADER_BYTES + image.length);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(SESSION), 1);
	new DataView(frame.buffer).setUint32(17, 70000, false);
	frame.set(image, FRAME_HEADER_BYTES);

	assert.deepEqual(parseGeneratedFrame(frame, SESSION), { revision: 70000, image });
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
	const frame = canvasFrame(SESSION, 1, new Uint8Array([7]));
	assert.equal(parseGeneratedFrame(frame, SESSION), null);
});

test('a frame shorter than the header is refused', () => {
	// 17 bytes is a complete pre-revision header and 20 stops inside the
	// revision, so neither can carry an image.
	for (const length of [17, FRAME_HEADER_BYTES - 1]) {
		const short = new Uint8Array(length);
		short[0] = GENERATED_FRAME;
		short.set(uuidBytes(SESSION), 1);

		assert.equal(parseGeneratedFrame(short, SESSION), null, `${length} bytes`);
	}
});

test('a header with no image is empty rather than null', () => {
	// The worker should not send one, but an empty payload is still our frame:
	// reporting null would make it indistinguishable from another session's.
	const frame = new Uint8Array(FRAME_HEADER_BYTES);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(SESSION), 1);
	new DataView(frame.buffer).setUint32(17, 70000, false);

	assert.deepEqual(parseGeneratedFrame(frame, SESSION), {
		revision: 70000,
		image: new Uint8Array(0)
	});
});

test('only a revision below the one already on screen is stale', () => {
	assert.equal(isStaleOutput(4, 5), true);
	assert.equal(isStaleOutput(0, 1), true);
	// Equal is not stale: one input may render more than once.
	assert.equal(isStaleOutput(5, 5), false);
	assert.equal(isStaleOutput(0, 0), false);
	assert.equal(isStaleOutput(6, 5), false);
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

test('the idle deadline is reached at 1500 ms, and a pending change holds the loop open', () => {
	assert.equal(shouldStopPolling(0, false), false);
	assert.equal(shouldStopPolling(IDLE_STOP_MS - 1, false), false);
	assert.equal(shouldStopPolling(IDLE_STOP_MS, false), true);
	assert.equal(shouldStopPolling(IDLE_STOP_MS + 1, false), true);
	// A change waiting for the next capture must not be stranded by a stop.
	assert.equal(shouldStopPolling(IDLE_STOP_MS, true), false);
	assert.equal(shouldStopPolling(IDLE_STOP_MS * 4, true), false);
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

test('the open control declares the 21 byte frame header this client reads back', () => {
	const message = JSON.parse(
		openMessage('vega-rt', 'a cat', { structure_strength: 0.5, steps: 10 })
	);
	assert.equal(message.frame_header, 2);
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

test('the update message carries a selection mask as it arrived, and null as null', () => {
	const mask: RealtimeCanvasMask = {
		polygons: [
			[
				[0.1, 0.2],
				[0.9, 0.2],
				[0.9, 0.8]
			]
		]
	};
	const message = JSON.parse(updateParamsMessage({ mask }));
	assert.equal(message.type, 'update_params');
	assert.deepEqual(message.params, { mask }, 'normalised numbers travel unrounded');
	assert.deepEqual(JSON.parse(updateParamsMessage({ mask: null })).params, { mask: null });
	// A mask rides beside the other params rather than replacing them.
	assert.deepEqual(JSON.parse(updateParamsMessage({ steps: 3, mask })).params, { steps: 3, mask });
});

test('a selection mask goes out on update_params and its echo moves no control', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	ready(socket);
	const mask: RealtimeCanvasMask = {
		polygons: [
			[
				[0, 0],
				[1, 0],
				[1, 1]
			]
		]
	};

	harness.session.updateParams({ mask });
	const sent = socket.sent.filter((data) => typeof data === 'string');
	const update = JSON.parse(sent[sent.length - 1] as string);
	assert.equal(update.type, 'update_params');
	assert.deepEqual(update.params, { mask });

	// The API echoes what it applied. The handler still reads only the keys it
	// read before masks existed, so a mask moves no slider in the panel.
	socket.message(JSON.stringify({ type: 'params_updated', params: { mask, steps: 12 } }));
	assert.deepEqual(harness.applied.at(-1), { steps: 12 });

	harness.session.updateParams({ mask: null });
	const cleared = JSON.parse(socket.sent[socket.sent.length - 1] as string);
	assert.deepEqual(cleared.params, { mask: null });
	harness.session.destroy();
});

test('a terminal close fails the session and anything else invites a reconnect', () => {
	for (const code of [4000, 4002, 4003, 4004, 4401, 4403]) {
		assert.equal(stateForCloseCode(code), 'failed', `code ${code}`);
	}
	// A normal close, or a dropped connection, keeps the canvas recoverable.
	assert.equal(stateForCloseCode(1000), 'interrupted');
	assert.equal(stateForCloseCode(1006), 'interrupted');
});

test('an unknown close code in the API range fails rather than invites the same close', () => {
	// 4005 and up are unassigned to this client (docs/connection-handling.md),
	// but they are the API's own range: a refusal it did not explain is still
	// one a reconnect would meet again.
	for (const code of [4005, 4006, 4010, 4400, 4402, 4500, 4999]) {
		assert.equal(stateForCloseCode(code), 'failed', `code ${code}`);
	}
	// Outside that range nothing changed: still a transport close to recover from.
	assert.equal(stateForCloseCode(3999), 'interrupted');
	assert.equal(stateForCloseCode(5000), 'interrupted');
});

test('a frame is held back only when the session limits say it is too large', () => {
	const limits: RealtimeCanvasLimits = { max_frame_bytes: 1_048_576 };
	assert.ok(frameFitsLimits(1_048_576, limits), 'the cap itself fits');
	assert.ok(!frameFitsLimits(1_048_577, limits));
	// An older API sends a ready without limits: no self-check, as before.
	assert.ok(frameFitsLimits(9_000_000, null));
});

test('limits come from the ready, and anything unusable means no self-check', () => {
	assert.deepEqual(
		readyLimits({
			max_frame_bytes: 1_048_576,
			formats: ['webp', 'png'],
			width: 512,
			height: 512
		}),
		{ max_frame_bytes: 1_048_576, formats: ['webp', 'png'], width: 512, height: 512 }
	);
	assert.equal(readyLimits(undefined), null);
	assert.equal(readyLimits({}), null);
	assert.equal(readyLimits({ max_frame_bytes: '1048576' }), null);
	assert.equal(readyLimits({ max_frame_bytes: 0 }), null);
	assert.equal(readyLimits('ready'), null);
});

test('a revoked session and a forbidden one are terminal, every other notice invites a retry', () => {
	const terminal: RealtimeCanvasNotice[] = ['session_revoked', 'refused_forbidden'];
	for (const notice of terminal) assert.ok(isTerminalNotice(notice), notice);
	const retryable: RealtimeCanvasNotice[] = [
		'',
		'encode_failed',
		'decode_failed',
		'frame_too_large',
		'socket_error',
		'refused_protocol',
		'refused_version',
		'refused_capacity',
		'refused_model'
	];
	for (const notice of retryable) assert.ok(!isTerminalNotice(notice), notice);
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
	queuePositions: number[];
	notices: string[];
	applied: Array<Record<string, unknown>>;
	counters: FrameCounters[];
	draws: number[];
	timerDelays: number[];
	tick(): void;
	timerCount(): number;
} {
	const sockets: TestSocket[] = [];
	const timers = new Map<number, () => void>();
	let nextTimer = 0;
	const timerDelays: number[] = [];
	const schedule = ((callback: () => void, delay = 0) => {
		const id = ++nextTimer;
		timers.set(id, callback);
		timerDelays.push(delay);
		return id;
	}) as typeof globalThis.setTimeout;
	const cancel = ((id: ReturnType<typeof setTimeout>) => {
		timers.delete(id as unknown as number);
	}) as typeof globalThis.clearTimeout;
	const states: ConnectionState[] = [];
	const queuePositions: number[] = [];
	const notices: string[] = [];
	const applied: Array<Record<string, unknown>> = [];
	const counters: FrameCounters[] = [];
	const draws: number[] = [];
	const session = createRealtimeCanvasSession({
		getDrawCanvas: () => ({}) as HTMLCanvasElement,
		getOutputCanvas: () => ({}) as HTMLCanvasElement,
		isCanvasBlank: options.isCanvasBlank ?? (() => false),
		onState: (state) => states.push(state),
		onQueuePosition: (position) => queuePositions.push(position),
		onNotice: (notice) => notices.push(notice),
		onCounters: (snapshot) => counters.push(snapshot),
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
		queuePositions,
		notices,
		applied,
		counters,
		draws,
		timerDelays,
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

/**
 * A generated frame for a session at a revision. Its single image byte repeats
 * the revision, so a test can tell which frame was decoded or drawn.
 */
function generated(id = SESSION, revision = 0): ArrayBuffer {
	const frame = new Uint8Array(FRAME_HEADER_BYTES + 1);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(id), 1);
	new DataView(frame.buffer).setUint32(17, revision, false);
	frame.set([revision & 0xff], FRAME_HEADER_BYTES);
	return frame.buffer;
}

/** The revision out of a frame this session sent, read as the wire does. */
function revisionOf(frame: Uint8Array): number {
	return new DataView(frame.buffer, frame.byteOffset, frame.byteLength).getUint32(17, false);
}

/** The frames (not the JSON controls) a socket was asked to send. */
function sentFrames(socket: TestSocket): Uint8Array[] {
	return socket.sent
		.filter((data) => typeof data !== 'string')
		.map((data) => new Uint8Array(data as ArrayBuffer));
}

function emptyGenerated(id = SESSION): ArrayBuffer {
	const frame = new Uint8Array(FRAME_HEADER_BYTES);
	frame[0] = GENERATED_FRAME;
	frame.set(uuidBytes(id), 1);
	return frame.buffer;
}

test('a busy socket retries at a bounded rate after a slow encode and keeps the latest drawing', async (context) => {
	let now = 0;
	context.mock.method(performance, 'now', () => now);
	let resolveEncode!: (image: Uint8Array<ArrayBuffer>) => void;
	let encodes = 0;
	let latest = 1;
	const harness = sessionHarness({
		encode: () => {
			encodes += 1;
			return encodes === 1
				? new Promise((resolve) => {
						resolveEncode = resolve;
					})
				: Promise.resolve(new Uint8Array([latest]));
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		harness.tick();
		latest = 2;
		harness.session.markChanged();
		socket.bufferedAmount = 1;
		now = 600;
		resolveEncode(new Uint8Array([1]));
		await Promise.resolve();
		assert.equal(harness.timerDelays.at(-1), 0, 'the first retry can start after an overrun');
		for (let index = 0; index < 20; index += 1) {
			harness.tick();
			const delay = harness.timerDelays.at(-1)!;
			assert.ok(delay >= 250 && delay <= 500, `blocked retry waited ${delay} ms`);
			assert.equal(harness.timerCount(), 1);
		}
		assert.equal(encodes, 1, 'a busy socket must not trigger another encode');
		assert.equal(socket.sent.filter((data) => typeof data !== 'string').length, 1);
		latest = 3;
		harness.session.markChanged();
		socket.bufferedAmount = 0;
		harness.tick();
		await Promise.resolve();
		const frames = socket.sent.filter((data) => typeof data !== 'string');
		assert.equal(frames.length, 2);
		assert.deepEqual(frames.at(-1), canvasFrame(SESSION, 2, new Uint8Array([3])));
		assert.equal(harness.timerCount(), 1);
	} finally {
		harness.session.destroy();
	}
	assert.equal(harness.timerCount(), 0);
});

test('a slow encode backs off the idle interval, stops on idle time, and wakes for a new edit', async (context) => {
	let now = 0;
	context.mock.method(performance, 'now', () => now);
	let encodes = 0;
	const harness = sessionHarness({
		encode: async () => {
			encodes += 1;
			now = 600;
			return new Uint8Array([encodes]);
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		harness.tick();
		await Promise.resolve();
		assert.equal(harness.timerDelays.at(-1), 0);

		// Three slow intervals are IDLE_STOP_MS, so the third idle tick is
		// the one that stops instead of arming.
		const stopTicks = IDLE_STOP_MS / SLOW_INTERVAL_MS;
		for (let index = 0; index < stopTicks - 1; index += 1) {
			harness.tick();
			const delay = harness.timerDelays.at(-1)!;
			assert.ok(delay >= 250 && delay <= 500, `idle retry waited ${delay} ms`);
			assert.equal(harness.timerCount(), 1);
		}
		harness.tick();
		assert.equal(harness.timerCount(), 0);

		harness.session.markChanged();
		assert.equal(harness.timerCount(), 1);
		// A stopped loop has no warm-up left to wait out.
		assert.equal(harness.timerDelays.at(-1), 0);
		harness.tick();
		await Promise.resolve();
		assert.equal(encodes, 2);
		assert.equal(socket.sent.filter((data) => typeof data !== 'string').length, 2);
	} finally {
		harness.session.destroy();
	}
});

test('the loop stops on idle time at either end of the band: six ticks fast, three slow', async (context) => {
	let now = 0;
	context.mock.method(performance, 'now', () => now);
	/** Idle ticks a session armed before it stopped, at one encode cost. */
	const idleTicksUntilStop = async (costMs: number): Promise<number> => {
		now = 0;
		const harness = sessionHarness({
			encode: async () => {
				now += costMs;
				return new Uint8Array([1]);
			}
		});
		try {
			harness.session.connect({
				modelId: 'vega-rt',
				prompt: 'a cat',
				params: { structure_strength: 0.5, steps: 10 }
			});
			ready(harness.sockets[0]);
			harness.tick();
			await Promise.resolve();
			let idleTicks = 0;
			while (harness.timerCount() > 0) {
				idleTicks += 1;
				assert.ok(idleTicks <= 10, `still arming after ${idleTicks} idle ticks`);
				harness.tick();
				await Promise.resolve();
			}
			return idleTicks;
		} finally {
			harness.session.destroy();
		}
	};
	// Six fast intervals and three slow ones are both IDLE_STOP_MS, so the
	// deadline is the same wall of time whichever end of the band is armed.
	assert.equal(await idleTicksUntilStop(0), IDLE_STOP_MS / FAST_INTERVAL_MS);
	assert.equal(await idleTicksUntilStop(600), IDLE_STOP_MS / SLOW_INTERVAL_MS);
});

test('a change during an idle stretch restarts the idle deadline', async () => {
	const harness = sessionHarness();
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		ready(harness.sockets[0]);
		harness.tick();
		await Promise.resolve();
		// Five idle ticks: 1250 ms, one short of the deadline.
		for (let tick = 0; tick < 5; tick += 1) {
			harness.tick();
			await Promise.resolve();
		}
		harness.session.markChanged();
		harness.tick();
		await Promise.resolve();
		// The change was sent, and the full deadline starts again after it:
		// without the reset the loop would stop on the very next idle tick.
		let idleTicks = 0;
		while (harness.timerCount() > 0) {
			idleTicks += 1;
			assert.ok(idleTicks <= 10, `still arming after ${idleTicks} idle ticks`);
			harness.tick();
			await Promise.resolve();
		}
		assert.equal(idleTicks, IDLE_STOP_MS / FAST_INTERVAL_MS);
	} finally {
		harness.session.destroy();
	}
});

test('a change after the loop stopped is captured at once, with no warm-up interval', async () => {
	const harness = sessionHarness({ isCanvasBlank: () => true });
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		for (let tick = 0; tick < IDLE_STOP_MS / FAST_INTERVAL_MS; tick += 1) harness.tick();
		assert.equal(harness.timerCount(), 0, 'the loop stopped while the canvas was blank');

		harness.session.markChanged();
		assert.equal(harness.timerCount(), 1);
		assert.equal(harness.timerDelays.at(-1), 0, 'the first change after idle waits out nothing');
		harness.tick();
		await Promise.resolve();
		assert.equal(sentFrames(socket).length, 1);
	} finally {
		harness.session.destroy();
	}
});

test('an encode failure keeps the drawing pending for a successful retry', async () => {
	let encodes = 0;
	const harness = sessionHarness({
		encode: async () => {
			encodes += 1;
			if (encodes === 1) throw new Error('encode');
			return new Uint8Array([2]);
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		harness.tick();
		await Promise.resolve();
		assert.equal(harness.notices.at(-1), 'encode_failed');
		assert.equal(socket.sent.filter((data) => typeof data !== 'string').length, 0);
		assert.equal(harness.timerCount(), 1);

		harness.tick();
		await Promise.resolve();
		assert.equal(encodes, 2);
		const frames = socket.sent.filter((data) => typeof data !== 'string');
		assert.equal(frames.length, 1);
		assert.deepEqual(frames[0], canvasFrame(SESSION, 1, new Uint8Array([2])));
	} finally {
		harness.session.destroy();
	}
});

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

test('one session numbers its frames 1, 2, 3 and keeps counting across controls', async () => {
	const harness = sessionHarness({
		encode: async () => new Uint8Array([1])
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		for (let index = 0; index < 3; index += 1) {
			if (index > 0) harness.session.markChanged();
			harness.tick();
			await Promise.resolve();
		}
		assert.deepEqual(sentFrames(socket).map(revisionOf), [1, 2, 3]);

		socket.message(JSON.stringify({ type: 'interrupted' }));
		socket.message(JSON.stringify({ type: 'resumed' }));
		harness.tick();
		await Promise.resolve();
		assert.deepEqual(sentFrames(socket).map(revisionOf), [1, 2, 3, 4]);

		// Queuing, a params change and the resume after them restart the loop,
		// not the count.
		socket.message(JSON.stringify({ type: 'queued', position: 1 }));
		socket.message(
			JSON.stringify({
				type: 'params_updated',
				params: { prompt: 'a dog', structure_strength: 0.7, steps: 12 }
			})
		);
		socket.message(JSON.stringify({ type: 'resumed' }));
		harness.tick();
		await Promise.resolve();

		assert.deepEqual(sentFrames(socket).map(revisionOf), [1, 2, 3, 4, 5]);
	} finally {
		harness.session.destroy();
	}
});

test('a second change before the capture is folded into one frame, and the fold is counted', async () => {
	const harness = sessionHarness({ isCanvasBlank: () => true });
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		harness.session.markChanged();
		assert.equal(harness.counters.at(-1)?.replaced, 0, 'a first change is not a replacement');
		harness.session.markChanged();
		assert.equal(
			harness.counters.at(-1)?.replaced,
			1,
			'a change on top of a waiting one folds into it'
		);

		harness.tick();
		await Promise.resolve();
		assert.equal(sentFrames(socket).length, 1, 'the next capture sends once');
		assert.equal(harness.counters.at(-1)?.replaced, 1, 'folding does not repeat itself');
	} finally {
		harness.session.destroy();
	}
});

test('one send and receive round trip moves each stage counter exactly once', async () => {
	const harness = sessionHarness();
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);
		harness.tick();
		await Promise.resolve();
		assert.deepEqual(harness.counters.at(-1), {
			attempted: 1,
			replaced: 0,
			encoded: 1,
			sent: 1,
			generated: 0,
			presented: 0
		});

		socket.message(generated(SESSION, 1));
		await Promise.resolve();
		assert.deepEqual(harness.counters.at(-1), {
			attempted: 1,
			replaced: 0,
			encoded: 1,
			sent: 1,
			generated: 1,
			presented: 1
		});

		// Arrival is counted ahead of the stale check, so a frame too old to
		// draw is still a frame this session received.
		socket.message(generated(SESSION, 0));
		await Promise.resolve();
		assert.deepEqual(harness.counters.at(-1), {
			attempted: 1,
			replaced: 0,
			encoded: 1,
			sent: 1,
			generated: 2,
			presented: 1
		});
	} finally {
		harness.session.destroy();
	}
});

test('a queued session reports each position and becomes active on ready', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	socket.open();
	socket.message(JSON.stringify({ type: 'queued', position: 2 }));
	assert.deepEqual(harness.states, ['connecting', 'queued']);
	assert.deepEqual(harness.queuePositions, [2]);
	socket.message(JSON.stringify({ type: 'queued', position: 1 }));
	assert.deepEqual(harness.queuePositions, [2, 1]);
	socket.message(JSON.stringify({ type: 'ready', session_id: SESSION }));
	assert.equal(harness.states.at(-1), 'active');
});

test('a live session queued after an idle release resumes and resends the whole canvas', async () => {
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
	const socket = harness.sockets[0];
	ready(socket);
	assert.equal(harness.states.at(-1), 'active');
	assert.deepEqual(harness.queuePositions, []);
	socket.message(JSON.stringify({ type: 'queued', position: 4 }));
	assert.equal(harness.states.at(-1), 'queued');
	assert.deepEqual(harness.queuePositions, [4]);
	socket.message(JSON.stringify({ type: 'resumed' }));
	assert.equal(harness.states.at(-1), 'active');
	harness.tick();
	await Promise.resolve();
	assert.equal(encodes, 1);
	const frames = socket.sent.filter((data) => typeof data !== 'string');
	assert.equal(frames.length, 1);
	assert.deepEqual(
		new Uint8Array(frames[0] as ArrayBuffer).subarray(FRAME_HEADER_BYTES),
		new Uint8Array([1])
	);
});

test('a queued control with an invalid position changes nothing', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	socket.open();
	// JSON.stringify drops an undefined position, which is the missing case.
	for (const position of [0, -1, 1.5, '2', undefined]) {
		socket.message(JSON.stringify({ type: 'queued', position }));
	}
	assert.deepEqual(harness.states, ['connecting']);
	assert.deepEqual(harness.queuePositions, []);
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

test('a keepalive is traffic the session never shows or acts on', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	ready(socket);
	const states = [...harness.states];
	const notices = [...harness.notices];
	const timers = harness.timerCount();

	socket.message(JSON.stringify({ type: 'keepalive' }));

	assert.deepEqual(harness.states, states);
	assert.deepEqual(harness.notices, notices);
	assert.equal(harness.timerCount(), timers);
	assert.equal(socket.sent.filter((data) => typeof data !== 'string').length, 0);
	harness.session.destroy();
});

test('a canvas image over the ready limit is never sent and says so', async () => {
	let payload = 4;
	const harness = sessionHarness({
		encode: async () => new Uint8Array(payload)
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	socket.open();
	socket.message(
		JSON.stringify({
			type: 'ready',
			session_id: SESSION,
			limits: { max_frame_bytes: 3, formats: ['webp', 'png'], width: 512, height: 512 }
		})
	);
	harness.tick();
	await Promise.resolve();
	assert.equal(socket.sent.filter((data) => typeof data !== 'string').length, 0);
	assert.equal(harness.notices.at(-1), 'frame_too_large');
	assert.equal(harness.states.at(-1), 'active');

	// A drawing that fits again is sent, and the notice clears with it.
	payload = 3;
	harness.session.markChanged();
	harness.tick();
	await Promise.resolve();
	assert.equal(socket.sent.filter((data) => typeof data !== 'string').length, 1);
	assert.equal(harness.notices.at(-1), '');
	harness.session.destroy();
});

test('an older API without limits sends every encoded image', async () => {
	const harness = sessionHarness({
		encode: async () => new Uint8Array(4_000_000)
	});
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	harness.tick();
	await Promise.resolve();
	const frames = harness.sockets[0].sent.filter((data) => typeof data !== 'string');
	assert.equal(frames.length, 1);
	assert.equal(harness.notices.at(-1), '');
	harness.session.destroy();
});

test('a 4005 refusal shows the frame_too_large notice without failing the session', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	const socket = harness.sockets[0];
	ready(socket);
	socket.message(
		JSON.stringify({ type: 'error', code: 4005, message: 'canvas frame exceeds 1048576 bytes' })
	);
	assert.equal(harness.notices.at(-1), 'frame_too_large');
	assert.equal(harness.states.at(-1), 'active');
	assert.equal(harness.timerCount(), 1, 'the capture loop keeps running');
	harness.session.destroy();
});

test('a revoked session and a forbidden one fail with their own notices', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	// The API sends the error before it closes, as docs/connection-handling.md
	// specifies for a revoked session.
	harness.sockets[0].message(
		JSON.stringify({ type: 'error', code: 4401, message: 'session revoked' })
	);
	harness.sockets[0].close(4401);
	assert.equal(harness.states.at(-1), 'failed');
	assert.equal(harness.notices.at(-1), 'session_revoked');

	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	harness.sockets[1].message(JSON.stringify({ type: 'error', code: 4403, message: 'forbidden' }));
	harness.sockets[1].close(4403);
	assert.equal(harness.states.at(-1), 'failed');
	assert.equal(harness.notices.at(-1), 'refused_forbidden');
});

test('a revoked close without a prior error still names the revocation', () => {
	const harness = sessionHarness();
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	harness.sockets[0].close(4401);
	assert.equal(harness.states.at(-1), 'failed');
	assert.equal(harness.notices.at(-1), 'session_revoked');
});

test('params acknowledgement updates controls and wakes capture', () => {
	const harness = sessionHarness({ isCanvasBlank: () => true });
	harness.session.connect({
		modelId: 'vega-rt',
		prompt: 'a cat',
		params: { structure_strength: 0.5, steps: 10 }
	});
	ready(harness.sockets[0]);
	for (let tick = 0; tick < IDLE_STOP_MS / FAST_INTERVAL_MS; tick += 1) harness.tick();
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
	assert.equal(harness.counters.at(-1)?.presented, 0);
});

test('a frame below the shown revision is not decoded, drawn or counted', async () => {
	let decodes = 0;
	const harness = sessionHarness({
		decode: async () => {
			decodes += 1;
			return { close() {} };
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);

		socket.message(generated(SESSION, 5));
		await Promise.resolve();
		assert.equal(harness.draws.length, 1);
		assert.equal(harness.counters.at(-1)?.presented, 1);

		// 4 arrived after 5 was on screen: an old worker response.
		socket.message(generated(SESSION, 4));
		await Promise.resolve();
		assert.equal(decodes, 1, 'a stale frame must not be decoded');
		assert.equal(harness.draws.length, 1, 'a stale frame must not be drawn');
		assert.equal(harness.counters.at(-1)?.presented, 1, 'a stale frame must not be shown');

		// Equal is not stale: the same input can render a second time.
		socket.message(generated(SESSION, 5));
		await Promise.resolve();
		assert.equal(decodes, 2, 'an equal revision is not dropped');
		assert.equal(harness.draws.length, 2, 'an equal revision draws again');
		assert.equal(harness.counters.at(-1)?.presented, 2);
	} finally {
		harness.session.destroy();
	}
});

test('a frame that goes stale while it waits its turn is dropped before the decode', async () => {
	const decoded: number[] = [];
	const harness = sessionHarness({
		decode: async (image) => {
			decoded.push(image[0]);
			return { close() {} };
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);

		// 4 arrives while 6 is decoding, so nothing is behind the screen for
		// it yet; by the time the loop reaches it, 6 has been drawn over it.
		socket.message(generated(SESSION, 6));
		socket.message(generated(SESSION, 4));
		await Promise.resolve();
		await Promise.resolve();

		assert.deepEqual(decoded, [6]);
		assert.equal(harness.draws.length, 1);
		assert.equal(harness.counters.at(-1)?.presented, 1);
	} finally {
		harness.session.destroy();
	}
});

test('a stale frame cannot take the slot of a newer one still waiting', async () => {
	const decoded: number[] = [];
	const harness = sessionHarness({
		decode: async (image) => {
			decoded.push(image[0]);
			return { close() {} };
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);

		socket.message(generated(SESSION, 5));
		socket.message(generated(SESSION, 6));
		await Promise.resolve();
		// 7 waits in the slot while 6 decodes; the late 4 must not push it out.
		socket.message(generated(SESSION, 7));
		socket.message(generated(SESSION, 4));
		await Promise.resolve();
		await Promise.resolve();

		assert.deepEqual(decoded, [5, 6, 7]);
		assert.equal(harness.draws.length, 3);
		assert.equal(harness.counters.at(-1)?.presented, 3);
	} finally {
		harness.session.destroy();
	}
});

test('a newer frame replaces the pending one while an older still decodes', async () => {
	const decoded: number[] = [];
	const harness = sessionHarness({
		decode: async (image) => {
			decoded.push(image[0]);
			return { close() {} };
		}
	});
	try {
		harness.session.connect({
			modelId: 'vega-rt',
			prompt: 'a cat',
			params: { structure_strength: 0.5, steps: 10 }
		});
		const socket = harness.sockets[0];
		ready(socket);

		// The first frame is in decode when the next two arrive, so 7 is
		// replaced in the slot by 8 and only the newest waits behind the decode.
		socket.message(generated(SESSION, 6));
		socket.message(generated(SESSION, 7));
		socket.message(generated(SESSION, 8));
		await Promise.resolve();
		await Promise.resolve();

		assert.deepEqual(decoded, [6, 8]);
		assert.equal(harness.draws.length, 2);
		assert.equal(harness.counters.at(-1)?.presented, 2);
	} finally {
		harness.session.destroy();
	}
});
