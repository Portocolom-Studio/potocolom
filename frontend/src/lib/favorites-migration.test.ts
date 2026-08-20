import assert from 'node:assert/strict';
import { test } from 'node:test';

import { planFavoriteMigration, type StarOutcome } from './favorites-migration.ts';

const plan = (...outcomes: Array<readonly [string, StarOutcome]>) =>
	planFavoriteMigration(outcomes);

test('a run where everything migrated retries nothing', () => {
	const result = plan(['a', 'migrated'], ['b', 'migrated']);
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 0);
});

test('an id that was never a job id is dropped and counted missing', () => {
	const result = plan(['a', 'migrated'], ['not-a-uuid', 'invalid']);
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 1);
});

test('every id answering 404 retries the whole list instead of discarding it', () => {
	const result = plan(['a', 'not-found'], ['b', 'not-found'], ['c', 'not-found']);
	assert.deepEqual(result.retry, ['a', 'b', 'c']);
	assert.equal(result.missing, 0);
});

test('a 404 alongside a migrated sibling is counted missing, not retried', () => {
	const result = plan(['a', 'migrated'], ['b', 'not-found']);
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 1);
});

test('an unanswered request is retried and never counted missing', () => {
	const result = plan(['a', 'failed']);
	assert.deepEqual(result.retry, ['a']);
	assert.equal(result.missing, 0);
});

test('a migrated id is never retried, so unstarring it survives a reload', () => {
	const result = plan(['a', 'migrated'], ['b', 'failed']);
	assert.deepEqual(result.retry, ['b']);
});

test('an empty run retries nothing', () => {
	const result = plan();
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 0);
});
