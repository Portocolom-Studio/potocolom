// node --test with the built in type stripping, the same as the other tests
// here (see Makefile verify-frontend). These pin how far one arrow key press
// moves a parameter, which is what issue #250 was about.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { normToValue, trackSteps, valueToNorm, type ParamRange } from './model-params.ts';

const steps: ParamRange = { min: 2, max: 8, default: 4, step: 1, integer: true };
const guidance: ParamRange = { min: 1, max: 20, default: 7, step: 0.5, integer: false };

test('a narrow integer parameter gets one notch per step', () => {
	// steps spans 2 to 8, so six presses cross it. On the old fixed 0-100 track
	// a press moved 0.06 of a step and about seventeen were needed to get from
	// 2 to 3.
	assert.equal(trackSteps(steps), 6);
});

test('one notch of a fractional parameter is one step of it', () => {
	assert.equal(trackSteps(guidance), 38);
	const oneNotch = normToValue(1 / trackSteps(guidance), guidance);
	assert.equal(oneNotch, guidance.min + guidance.step);
});

test('notches walk the whole range, one value at a time', () => {
	const notches = trackSteps(steps);
	const walked = Array.from({ length: notches + 1 }, (_, index) =>
		normToValue(index / notches, steps)
	);
	assert.deepEqual(walked, [2, 3, 4, 5, 6, 7, 8]);
});

test('a degenerate spec yields a track rather than a division by zero', () => {
	assert.equal(trackSteps({ min: 1, max: 1, default: 1, step: 1, integer: true }), 1);
	assert.equal(trackSteps({ min: 0, max: 10, default: 0, step: 0, integer: false }), 1);
});

test('a pathological range cannot grow the track without bound', () => {
	// The slider holds an array with one entry per notch, and a manifest
	// declares the range, so the count is capped at the old fixed track.
	assert.equal(trackSteps({ min: 0, max: 100000, default: 0, step: 0.001, integer: false }), 100);
});

test('a value sits on the notch that reproduces it', () => {
	const notches = trackSteps(steps);
	assert.equal(Math.round(valueToNorm(5, steps) * notches), 3);
	assert.equal(normToValue(3 / notches, steps), 5);
});
