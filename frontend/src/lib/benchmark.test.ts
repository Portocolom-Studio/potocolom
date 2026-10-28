import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	categoryLineSeries,
	leaderboardRows,
	mayReadSessions,
	type ModelStats
} from './benchmark.ts';

test('category plots omit models without measured samples', () => {
	assert.deepEqual(
		categoryLineSeries(
			[
				{
					prompt_id: 1,
					title: 'Skipped scene',
					category: 'test',
					model_id: 'skipped-model',
					variant: 'default',
					params: {},
					state: 'skipped'
				}
			],
			['skipped-model']
		),
		{ categories: [], series: [] }
	);
});

test('leaderboard omits models without successful timings', () => {
	const skippedOnly: ModelStats = {
		model_id: 'skipped-model',
		succeeded: 0,
		failed: 0,
		skipped: 3,
		load_ms: null,
		avg_gpu_ms: null,
		median_gpu_ms: null,
		avg_wall_s: 0
	};
	const measured: ModelStats = {
		model_id: 'measured-model',
		succeeded: 2,
		failed: 0,
		skipped: 1,
		load_ms: 500,
		avg_gpu_ms: 1000,
		median_gpu_ms: 900,
		avg_wall_s: 1.25
	};

	assert.deepEqual(leaderboardRows([skippedOnly, measured]), [
		{
			rank: 1,
			model_id: 'measured-model',
			gpu_ms: 1000,
			wall_s: 1.25,
			load_ms: 500,
			gpu_display: '1.0 s',
			wall_display: '1.3 s',
			load_display: '500 ms',
			gpu_ratio: 1,
			wall_ratio: 1,
			load_ratio: 1
		}
	]);
});

test('AUTH_MODE=none asks: empty auth_methods means the implicit user is admin', () => {
	assert.equal(mayReadSessions({ authMethods: [], hasCsrf: false, role: null }), true);
});

test('accounts mode with no session asks nothing', () => {
	assert.equal(mayReadSessions({ authMethods: ['password'], hasCsrf: false, role: null }), false);
});

test('accounts mode signed in as admin asks', () => {
	assert.equal(mayReadSessions({ authMethods: ['password'], hasCsrf: true, role: 'admin' }), true);
});

test('accounts mode signed in as a non-admin does not ask', () => {
	assert.equal(mayReadSessions({ authMethods: ['password'], hasCsrf: true, role: 'user' }), false);
	assert.equal(
		mayReadSessions({ authMethods: ['password'], hasCsrf: true, role: 'viewer' }),
		false
	);
});

test('a failed config probe does not ask', () => {
	assert.equal(mayReadSessions({ authMethods: null, hasCsrf: false, role: null }), false);
});
