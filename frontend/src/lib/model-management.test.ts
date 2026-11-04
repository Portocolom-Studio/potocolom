import { test } from 'node:test';
import assert from 'node:assert/strict';
import { selectionSpace, shouldPoll, formatSize } from './model-management.ts';

const gib = 1024 ** 3;
test('selection space reserves five GiB and rejects unknown sizes', () => {
	assert.deepEqual(selectionSpace(10 * gib, [2 * gib, 3 * gib]), {
		total: 5 * gib,
		freeAfter: 5 * gib,
		allowed: true
	});
	assert.equal(selectionSpace(10 * gib, [6 * gib]).allowed, false);
	assert.equal(selectionSpace(10 * gib, [null]).allowed, false);
});
test('formats binary sizes for English and Spanish', () => {
	assert.equal(formatSize(1.5 * gib, 'en'), '1.50 GiB');
	assert.equal(formatSize(1.5 * gib, 'es'), '1,50 GiB');
});
test('polls only active downloads', () => {
	assert.equal(shouldPoll(['queued', 'done']), true);
	assert.equal(shouldPoll(['downloading']), true);
	assert.equal(shouldPoll(['failed', 'done']), false);
});

test('reserves remaining download bytes before checking selection', () => {
	assert.deepEqual(selectionSpace(12 * gib, [4 * gib], 6 * gib), {
		total: 4 * gib,
		freeAfter: 2 * gib,
		allowed: false
	});
});

test('poll interval follows active work and stops after unmount', async () => {
	const { pollInterval } = await import('./model-management.ts');
	assert.equal(pollInterval(true, false), 10000);
	assert.equal(pollInterval(true, true), 1000);
	assert.equal(pollInterval(false, true), null);
});

test('first-run suggestions include only fitting, available models', async () => {
	const { initialSelection } = await import('./model-management.ts');
	const models = [
		{ id: 'suggested', suggested: true, installed: false, needs_token: false, fits: true },
		{ id: 'large', suggested: true, installed: false, needs_token: false, fits: false },
		{ id: 'gated', suggested: true, installed: false, needs_token: true, fits: true },
		{ id: 'other', suggested: false, installed: false, needs_token: false, fits: true }
	];
	assert.deepEqual(initialSelection(models), ['suggested']);
	assert.deepEqual(initialSelection([...models, { ...models[3], installed: true }]), []);
});

test('disk preview accounts separately for reserved and selected space', async () => {
	const { diskPreview } = await import('./model-management.ts');
	assert.deepEqual(diskPreview(20 * gib, 12 * gib, 6 * gib, gib), {
		used: 8 * gib,
		reserved: 6 * gib,
		selected: gib,
		freeAfter: 5 * gib
	});
});

test('unknown sizes propagate through reservations and available space', async () => {
	const { knownSize, remainingReservation, sumKnown } = await import('./model-management.ts');
	assert.equal(knownSize(null), null);
	assert.equal(knownSize(0), 0);
	assert.equal(knownSize(undefined), null);
	assert.equal(remainingReservation(0, null, 0), null);
	assert.equal(remainingReservation(0, 0, 0), 0);
	assert.equal(remainingReservation(0, 3 * gib, gib), 2 * gib);
	assert.equal(remainingReservation(3 * gib, null, gib), 2 * gib);
	assert.equal(sumKnown([gib, null]), null);
	assert.deepEqual(selectionSpace(7 * gib, [gib], sumKnown([remainingReservation(0, null, 0)])), {
		total: gib,
		freeAfter: null,
		allowed: false
	});
	assert.deepEqual(selectionSpace(null, [gib]), { total: gib, freeAfter: null, allowed: false });
	assert.deepEqual(selectionSpace(7 * gib, [0]), { total: 0, freeAfter: 7 * gib, allowed: true });
	assert.deepEqual(selectionSpace(7 * gib, [0, gib]), {
		total: gib,
		freeAfter: 6 * gib,
		allowed: true
	});
	assert.deepEqual(selectionSpace(7 * gib, [null, gib]), {
		total: null,
		freeAfter: null,
		allowed: false
	});
});
