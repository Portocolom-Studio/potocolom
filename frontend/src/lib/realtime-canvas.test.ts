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
	SLOW_INTERVAL_MS,
	canvasFrame,
	nextDelayMs,
	nextIntervalMs,
	openMessage,
	parseGeneratedFrame,
	shouldSendFrame,
	stateForCloseCode,
	uuidBytes
} from './realtime-canvas.ts';

const SESSION = '3f2504e0-4f89-11d3-9a0c-0305e82c3301';
/** The same session with only the final byte changed. */
const NEARLY = '3f2504e0-4f89-11d3-9a0c-0305e82c3302';
/**
 * And with only the first byte changed. Both ends are needed: a comparison
 * weakened to any single byte passes as long as every fixture differs at that
 * one byte, so one near-miss fixture cannot pin the whole UUID.
 */
const NEARLY_FIRST = '402504e0-4f89-11d3-9a0c-0305e82c3301';

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
	for (const other of [NEARLY, NEARLY_FIRST]) {
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
		openMessage('vega-rt', '  a red house  ', { strength: 0.5, steps: 10 })
	);
	assert.equal(message.type, 'open');
	assert.equal(message.model_id, 'vega-rt');
	assert.deepEqual(message.params, { prompt: 'a red house', strength: 0.5, steps: 10 });
});

test('the open message carries the strength and steps passed to it', () => {
	const message = JSON.parse(openMessage('vega-rt', 'a cat', { strength: 0.25, steps: 30 }));
	assert.equal(message.params.strength, 0.25);
	assert.equal(message.params.steps, 30);
});

test('a refusal fails the session and anything else invites a reconnect', () => {
	for (const code of [4000, 4002, 4003, 4004]) {
		assert.equal(stateForCloseCode(code), 'failed', `code ${code}`);
	}
	// A normal close, or a dropped connection, keeps the canvas recoverable.
	assert.equal(stateForCloseCode(1000), 'interrupted');
	assert.equal(stateForCloseCode(1006), 'interrupted');
});
