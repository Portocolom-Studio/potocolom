import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	DISMISS_DISTANCE_RATIO,
	DISMISS_VELOCITY,
	lockGesture,
	releaseVelocity,
	shouldDismiss,
	swipeOffset
} from './swipe-dismiss.ts';

test('swipeOffset follows the finger toward the edge and resists the other way', () => {
	assert.equal(swipeOffset(80, 1), 80);
	assert.equal(swipeOffset(-80, -1), -80);
	assert.equal(swipeOffset(0, 1), 0);
	assert.ok(Math.abs(swipeOffset(-80, 1)) < 80);
	assert.ok(swipeOffset(-80, 1) < 0);
	assert.ok(swipeOffset(80, -1) > 0 && swipeOffset(80, -1) < 80);
});

test('shouldDismiss closes past the distance threshold', () => {
	const size = 300;
	const past = size * DISMISS_DISTANCE_RATIO + 1;
	assert.equal(shouldDismiss({ offset: past, size, velocity: 0, direction: 1 }), true);
	assert.equal(shouldDismiss({ offset: -past, size, velocity: 0, direction: -1 }), true);
	assert.equal(shouldDismiss({ offset: past - 20, size, velocity: 0, direction: 1 }), false);
});

test('shouldDismiss closes on a flick toward the edge', () => {
	const fast = DISMISS_VELOCITY + 0.1;
	assert.equal(shouldDismiss({ offset: 30, size: 300, velocity: fast, direction: 1 }), true);
	assert.equal(shouldDismiss({ offset: -30, size: 300, velocity: -fast, direction: -1 }), true);
	// A flick back toward the screen keeps it open even after some travel.
	assert.equal(shouldDismiss({ offset: 60, size: 300, velocity: -fast, direction: 1 }), false);
	// A jittery tap is not a flick.
	assert.equal(shouldDismiss({ offset: 2, size: 300, velocity: fast, direction: 1 }), false);
});

test('shouldDismiss never closes a panel pulled the wrong way', () => {
	assert.equal(shouldDismiss({ offset: -200, size: 300, velocity: 5, direction: 1 }), false);
	assert.equal(shouldDismiss({ offset: 0, size: 300, velocity: 5, direction: 1 }), false);
});

test('lockGesture waits, then tells a swipe from a scroll', () => {
	assert.equal(lockGesture(3, 2, 1), null);
	assert.equal(lockGesture(20, 3, 1), 'swipe');
	assert.equal(lockGesture(-20, 3, -1), 'swipe');
	assert.equal(lockGesture(3, 20, 1), 'other');
	// Sideways but away from the edge is not this panel's gesture.
	assert.equal(lockGesture(-20, 3, 1), 'other');
	// A diagonal leaning vertical stays a scroll.
	assert.equal(lockGesture(12, 11, 1), 'other');
});

test('releaseVelocity uses the recent samples only', () => {
	assert.equal(releaseVelocity([], 100), 0);
	assert.equal(releaseVelocity([{ x: 0, t: 0 }], 10), 0);
	assert.equal(
		releaseVelocity(
			[
				{ x: 0, t: 0 },
				{ x: 50, t: 50 }
			],
			60
		),
		1
	);
	// A pause before release leaves no recent movement.
	assert.equal(
		releaseVelocity(
			[
				{ x: 0, t: 0 },
				{ x: 50, t: 50 }
			],
			400
		),
		0
	);
	assert.equal(
		releaseVelocity(
			[
				{ x: 100, t: 900 },
				{ x: 40, t: 960 }
			],
			980
		),
		-1
	);
});
