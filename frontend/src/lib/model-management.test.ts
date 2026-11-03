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
test('formats binary sizes', () => assert.equal(formatSize(gib), '1.00 GiB'));
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
