// node --test with the built in type stripping, the same as the other tests
// here (see Makefile verify-frontend). These pin how far one arrow key press
// moves a parameter, which is what issue #250 was about, and how a manifest is
// read for the realtime canvas edit prompt (issue #376).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	MAX_TRACK_NOTCHES,
	modelAcceptsEditPrompt,
	normToValue,
	trackSteps,
	valueToNorm,
	type ModelParamProperty,
	type ParamRange
} from './model-params.ts';
import type { Model } from './studio.svelte.ts';

const steps: ParamRange = { min: 2, max: 8, default: 4, step: 1, integer: true };
const guidance: ParamRange = { min: 1, max: 20, default: 7, step: 0.5, integer: false };

/** A realtime model whose manifest declares `properties` and nothing else,
 * because the manifest is the whole subject of the tests below. */
function modelDeclaring(properties: Record<string, unknown>): Model {
	return {
		id: 'fixture',
		name: 'Fixture',
		capabilities: ['realtime'],
		min_vram_gb: 8,
		default: true,
		estimated_gpu_ms_default: null,
		parameters: { properties: properties as Record<string, ModelParamProperty> }
	};
}

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
	// declares the range, so the count is bounded.
	const absurd: ParamRange = { min: 0, max: 100000, default: 0, step: 0.001, integer: false };
	assert.equal(trackSteps(absurd), MAX_TRACK_NOTCHES);
});

test('the cap clears every range a model could reasonably declare', () => {
	// Past the cap a press moves more than one step, so the bound has to sit
	// where that stops mattering. These are the shapes a manifest plausibly
	// declares; each keeps one press to one step.
	const cases: ParamRange[] = [
		{ min: 0, max: 1, default: 0.5, step: 0.0005, integer: false },
		{ min: 0, max: 100, default: 50, step: 0.05, integer: false },
		{ min: 1, max: 150, default: 30, step: 1, integer: true },
		{ min: 1, max: 20, default: 7, step: 0.5, integer: false }
	];
	for (const spec of cases) {
		const notches = trackSteps(spec);
		// At the cap exactly, a press is still one step; past it, it is not.
		assert.ok(notches <= MAX_TRACK_NOTCHES, `${spec.max} by ${spec.step} exceeded the cap`);
		assert.equal(normToValue(1 / notches, spec), spec.min + spec.step);
	}
});

test('a value sits on the notch that reproduces it', () => {
	const notches = trackSteps(steps);
	assert.equal(Math.round(valueToNorm(5, steps) * notches), 3);
	assert.equal(normToValue(3 / notches, steps), 5);
});

// The edit prompt field of the realtime canvas (issue #376) mounts only for a
// manifest whose mask takes a prompt of its own, so the shapes a manifest can
// plausibly take each get their own answer.
const MASK_WITH_PROMPT = {
	type: 'object',
	properties: {
		polygons: { type: 'array' },
		prompt: { type: 'string' }
	}
};

test('a model whose mask declares a prompt accepts an edit prompt', () => {
	assert.equal(modelAcceptsEditPrompt(modelDeclaring({ mask: MASK_WITH_PROMPT })), true);
});

test('a model with no mask declared accepts no edit prompt', () => {
	assert.equal(modelAcceptsEditPrompt(modelDeclaring({ prompt: { type: 'string' } })), false);
	assert.equal(modelAcceptsEditPrompt(undefined), false);
});

test('a mask without a prompt under it accepts no edit prompt', () => {
	// The shipped manifest before this field existed: polygons and nothing else.
	assert.equal(
		modelAcceptsEditPrompt(
			modelDeclaring({ mask: { type: 'object', properties: { polygons: { type: 'array' } } } })
		),
		false
	);
	// A mask that is not an object at all, as the canvas smoke model declares.
	assert.equal(modelAcceptsEditPrompt(modelDeclaring({ mask: { type: 'string' } })), false);
});

test('the anyOf form of a mask is read through its object branch', () => {
	// sdxl-turbo and vega-rt declare the mask anyOf-nullable, because
	// mask: null clears the selection.
	const nullable = (object: unknown): Record<string, unknown> => ({
		anyOf: [{ type: 'null' }, object]
	});
	assert.equal(
		modelAcceptsEditPrompt(modelDeclaring({ mask: nullable(MASK_WITH_PROMPT) })),
		true,
		'the object branch carries the prompt'
	);
	assert.equal(
		modelAcceptsEditPrompt(
			modelDeclaring({
				mask: nullable({ type: 'object', properties: { polygons: { type: 'array' } } })
			})
		),
		false,
		'the object branch has no prompt'
	);
	assert.equal(
		modelAcceptsEditPrompt(modelDeclaring({ mask: { anyOf: [{ type: 'null' }] } })),
		false,
		'there is no object branch at all'
	);
});
