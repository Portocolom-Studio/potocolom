import assert from 'node:assert/strict';
import { test } from 'node:test';

import en from './i18n/en.json';
import es from './i18n/es.json';
import {
	markTourDone,
	settleStep,
	shouldAutoStart,
	stepFrom,
	STUDIO_TOUR_STEPS,
	TOUR_DONE_KEY
} from './studio-tour.ts';

// Steps by index: 0 canvas, 1 prompt, 2 model, 3 generate, 4 install, 5 images.
const everyControlShown = () => true;

function onlyShown(...anchors: string[]): (anchor: string) => boolean {
	return (anchor) => anchors.includes(anchor);
}

function memoryStorage(values = new Map<string, string>()) {
	return {
		getItem: (key: string) => values.get(key) ?? null,
		setItem: (key: string, value: string) => void values.set(key, value),
		removeItem: (key: string) => void values.delete(key)
	};
}

test('next and back walk the steps in order and end at either edge', () => {
	let step = stepFrom(STUDIO_TOUR_STEPS, -1, 1, everyControlShown);
	assert.equal(step, 0);
	step = stepFrom(STUDIO_TOUR_STEPS, step, 1, everyControlShown);
	assert.equal(step, 1);
	step = stepFrom(STUDIO_TOUR_STEPS, step, -1, everyControlShown);
	assert.equal(step, 0);
	assert.equal(
		stepFrom(STUDIO_TOUR_STEPS, 0, -1, everyControlShown),
		-1,
		'no step before the first'
	);
	assert.equal(stepFrom(STUDIO_TOUR_STEPS, 5, 1, everyControlShown), -1, 'no step after the last');
});

test('a step whose control is not on screen is skipped in both directions', () => {
	const shown = onlyShown('canvas', 'prompt', 'generate');
	assert.equal(
		stepFrom(STUDIO_TOUR_STEPS, 1, 1, shown),
		3,
		'model is missing, so generate follows prompt'
	);
	assert.equal(stepFrom(STUDIO_TOUR_STEPS, 3, -1, shown), 1, 'back from generate lands on prompt');
	assert.equal(stepFrom(STUDIO_TOUR_STEPS, 3, 1, shown), -1, 'images is missing, so the tour ends');
});

test('a missing first control starts the tour at the next one that is on screen', () => {
	const shown = onlyShown('prompt', 'images');
	assert.equal(stepFrom(STUDIO_TOUR_STEPS, -1, 1, shown), 1);
});

test('when no control is on screen there is no step to show', () => {
	assert.equal(
		stepFrom(STUDIO_TOUR_STEPS, -1, 1, () => false),
		-1
	);
});

test('first visit with working storage auto-starts the tour', () => {
	assert.equal(
		shouldAutoStart(() => memoryStorage()),
		true
	);
});

test('the auto-start write probe leaves no key behind', () => {
	const values = new Map<string, string>();
	assert.equal(
		shouldAutoStart(() => memoryStorage(values)),
		true
	);
	assert.equal(values.size, 0);
});

test('readable storage that refuses writes never auto-starts', () => {
	const readOnly = () => ({
		getItem: () => null,
		setItem: () => {
			throw new Error('QuotaExceededError');
		},
		removeItem: () => {}
	});
	assert.equal(shouldAutoStart(readOnly), false);
});

test('a step whose control is still on screen stays put when the layout changes', () => {
	assert.equal(settleStep(STUDIO_TOUR_STEPS, 3, everyControlShown), 3);
});

test('a step whose control left the screen moves to the next one that is on screen', () => {
	assert.equal(settleStep(STUDIO_TOUR_STEPS, 3, onlyShown('canvas', 'prompt', 'images')), 5);
});

test('a step whose control and every later control left the screen ends the tour', () => {
	assert.equal(settleStep(STUDIO_TOUR_STEPS, 3, onlyShown('canvas', 'prompt')), -1);
	assert.equal(settleStep(STUDIO_TOUR_STEPS, 4, onlyShown('canvas', 'prompt')), -1);
});

test('a finished or skipped tour sets the flag, and the flag stops auto-start', () => {
	const values = new Map<string, string>();
	markTourDone(() => memoryStorage(values));
	assert.equal(values.get(TOUR_DONE_KEY), '1');
	assert.equal(
		shouldAutoStart(() => memoryStorage(values)),
		false
	);
});

test('blocked storage never auto-starts and never throws', () => {
	const blocked = () => {
		throw new Error('SecurityError');
	};
	assert.equal(shouldAutoStart(blocked), false);
	assert.doesNotThrow(() => markTourDone(blocked));
});

test('storage that reads but refuses writes still does not throw on finish', () => {
	const readOnly = () => ({
		getItem: () => null,
		setItem: () => {
			throw new Error('QuotaExceededError');
		},
		removeItem: () => {}
	});
	assert.doesNotThrow(() => markTourDone(readOnly));
});

test('every step has a title and body in English and Spanish, and each control is anchored once', () => {
	const anchors = new Set(STUDIO_TOUR_STEPS.map((step) => step.anchor));
	assert.equal(anchors.size, STUDIO_TOUR_STEPS.length);
	const dictionaries: Record<string, string>[] = [en, es];
	for (const step of STUDIO_TOUR_STEPS) {
		for (const dictionary of dictionaries) {
			assert.ok(dictionary[step.titleKey]?.trim(), `${step.titleKey} is missing`);
			assert.ok(dictionary[step.bodyKey]?.trim(), `${step.bodyKey} is missing`);
		}
	}
});
