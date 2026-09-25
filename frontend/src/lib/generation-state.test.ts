import assert from 'node:assert/strict';
import { test } from 'node:test';

import { isCancellable } from './generation-state.ts';

test('queued and running jobs are cancellable', () => {
	assert.equal(isCancellable('queued'), true);
	assert.equal(isCancellable('running'), true);
});

test('finished or unknown states are not cancellable', () => {
	assert.equal(isCancellable('succeeded'), false);
	assert.equal(isCancellable('failed'), false);
	assert.equal(isCancellable('cancelled'), false);
});
