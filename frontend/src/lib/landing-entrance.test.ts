import assert from 'node:assert/strict';
import { test } from 'node:test';

import { LANDING_ENTRANCE_TIMEOUT_MS, shouldRevealLandingEntrance } from './landing-entrance.ts';

test('the landing gate waits for the minimum spin even when assets are ready', () => {
	assert.equal(
		shouldRevealLandingEntrance({
			startedAtMs: 0,
			nowMs: 899,
			assetsLoaded: true,
			minimumSpinMs: 900,
			timeoutMs: 8000
		}),
		false
	);
	assert.equal(
		shouldRevealLandingEntrance({
			startedAtMs: 0,
			nowMs: 900,
			assetsLoaded: true,
			minimumSpinMs: 900,
			timeoutMs: 8000
		}),
		true
	);
});

test('the landing gate waits for slow assets past the minimum spin', () => {
	assert.equal(
		shouldRevealLandingEntrance({
			startedAtMs: 0,
			nowMs: 5000,
			assetsLoaded: false,
			minimumSpinMs: 900,
			timeoutMs: 8000
		}),
		false
	);
});

test('the landing gate releases at the absolute deadline with assets still pending', () => {
	assert.equal(
		shouldRevealLandingEntrance({
			startedAtMs: 0,
			nowMs: 8000,
			assetsLoaded: false,
			minimumSpinMs: 900,
			timeoutMs: 8000
		}),
		true
	);
});

test('the landing gate deadline is measured from the entrance start', () => {
	assert.equal(
		shouldRevealLandingEntrance({
			startedAtMs: 1000,
			nowMs: 9000,
			assetsLoaded: false,
			minimumSpinMs: 900,
			timeoutMs: 8000
		}),
		true
	);
	assert.equal(
		shouldRevealLandingEntrance({
			startedAtMs: 1000,
			nowMs: 8999,
			assetsLoaded: false,
			minimumSpinMs: 900,
			timeoutMs: 8000
		}),
		false
	);
});

test('the deadline constant is the named eight-second bound', () => {
	assert.equal(LANDING_ENTRANCE_TIMEOUT_MS, 8000);
});
