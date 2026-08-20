import assert from 'node:assert/strict';
import { test } from 'node:test';

import { runFavoriteMigration } from './favorites-migration.ts';

const ID_A = '11111111-1111-1111-1111-111111111111';
const ID_B = '22222222-2222-2222-2222-222222222222';
const ID_C = '33333333-3333-3333-3333-333333333333';

const answering = (byId: Record<string, number | null>) => {
	const asked: string[] = [];
	const star = async (id: string) => {
		asked.push(id);
		return byId[id] ?? null;
	};
	return { asked, star };
};

test('a run where every star returns 204 retries nothing', async () => {
	const { star } = answering({ [ID_A]: 204, [ID_B]: 204 });
	const result = await runFavoriteMigration([ID_A, ID_B], star);
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 0);
});

test('an id that was never a job id is dropped, counted missing, and never sent', async () => {
	const { asked, star } = answering({ [ID_A]: 204 });
	const result = await runFavoriteMigration([ID_A, 'not-a-uuid'], star);
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 1);
	assert.deepEqual(asked, [ID_A]);
});

test('an invalid id is counted missing even when nothing else migrated', async () => {
	const { star } = answering({ [ID_A]: 404 });
	const result = await runFavoriteMigration([ID_A, 'not-a-uuid'], star);
	assert.deepEqual(result.retry, [ID_A]);
	assert.equal(result.missing, 1);
});

test('every id answering 404 retries the whole list instead of discarding it', async () => {
	const { star } = answering({ [ID_A]: 404, [ID_B]: 404, [ID_C]: 404 });
	const result = await runFavoriteMigration([ID_A, ID_B, ID_C], star);
	assert.deepEqual(result.retry, [ID_A, ID_B, ID_C]);
	assert.equal(result.missing, 0);
});

test('a 404 beside a migrated sibling is still retried, never counted missing', async () => {
	const { star } = answering({ [ID_A]: 204, [ID_B]: 404 });
	const result = await runFavoriteMigration([ID_A, ID_B], star);
	assert.deepEqual(result.retry, [ID_B]);
	assert.equal(result.missing, 0);
});

test('a 404 beside an unanswered request is retried, whatever the order', async () => {
	const { star } = answering({ [ID_A]: 404, [ID_B]: null });
	assert.deepEqual((await runFavoriteMigration([ID_A, ID_B], star)).retry, [ID_A, ID_B]);
	assert.deepEqual((await runFavoriteMigration([ID_B, ID_A], star)).retry, [ID_B, ID_A]);
});

test('an unanswered request is retried and never counted missing', async () => {
	const { star } = answering({ [ID_A]: null });
	const result = await runFavoriteMigration([ID_A], star);
	assert.deepEqual(result.retry, [ID_A]);
	assert.equal(result.missing, 0);
});

test('only 204 counts as migrated, so a proxy 200 is retried rather than dropped', async () => {
	const { star } = answering({ [ID_A]: 200 });
	const result = await runFavoriteMigration([ID_A], star);
	assert.deepEqual(result.retry, [ID_A]);
	assert.equal(result.missing, 0);
});

test('a 403 is retried rather than dropped', async () => {
	const { star } = answering({ [ID_A]: 403 });
	assert.deepEqual((await runFavoriteMigration([ID_A], star)).retry, [ID_A]);
});

test('a migrated id is never retried, so unstarring it survives a reload', async () => {
	const { star } = answering({ [ID_A]: 204, [ID_B]: null });
	const result = await runFavoriteMigration([ID_A, ID_B], star);
	assert.deepEqual(result.retry, [ID_B]);
});

test('an empty run asks nothing and retries nothing', async () => {
	const { asked, star } = answering({});
	const result = await runFavoriteMigration([], star);
	assert.deepEqual(result.retry, []);
	assert.equal(result.missing, 0);
	assert.deepEqual(asked, []);
});
