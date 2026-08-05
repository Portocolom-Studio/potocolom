import assert from 'node:assert/strict';
import test from 'node:test';
import {
	LINEAGE_WORLD_LIMIT,
	clampLineageCoordinate,
	decideLineageTreeLoad,
	lineageTreeOmittedHistoryJobIds,
	lineageTreeNeedsHistoryRefresh,
	retainedRetryBudget,
	rebaseLineageViewport
} from './lineage-canvas-state.ts';
import type { Generation } from './studio.svelte.ts';

function generation(id: string, assetId: string | null, sourceAssetId: string | null): Generation {
	return {
		id,
		model_id: 'test-model',
		source_asset_id: sourceAssetId,
		params: {},
		state: assetId ? 'succeeded' : 'running',
		progress: null,
		gpu_ms: null,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: '2026-08-04T00:00:00Z',
		dispatched_at: null,
		finished_at: assetId ? '2026-08-04T00:00:01Z' : null,
		starred_at: null,
		expired_favorite: false,
		assets: assetId
			? [
					{
						id: assetId,
						url: '/asset',
						thumbnail_url: '/thumbnail',
						download_url: '/download',
						width: 512,
						height: 512
					}
				]
			: []
	};
}

test('cached tree is stale after a derivative completes while Images is unmounted', () => {
	const root = generation('root-job', 'root-asset', null);
	const cachedNodes = [
		{
			id: 'root-asset',
			data: { entry: { job_id: 'root-job', asset_id: 'root-asset' } }
		}
	];

	assert.equal(lineageTreeNeedsHistoryRefresh(cachedNodes, [root]), false);

	const runningDerivative = generation('child-job', null, 'root-asset');
	assert.equal(lineageTreeNeedsHistoryRefresh(cachedNodes, [runningDerivative, root]), false);

	const finishedDerivative = generation('child-job', 'child-asset', 'root-asset');
	assert.equal(lineageTreeNeedsHistoryRefresh(cachedNodes, [finishedDerivative, root]), true);
});

test('truncated tree ignores omitted children but refreshes changed retained nodes', () => {
	const root = generation('root-job', 'root-asset', null);
	const cachedNodes = [
		{
			id: 'root-asset',
			data: { entry: { job_id: 'root-job', asset_id: 'root-asset' } }
		}
	];
	const omittedChild = generation('omitted-job', 'omitted-asset', 'root-asset');
	const knownOmissions = lineageTreeOmittedHistoryJobIds(cachedNodes, [omittedChild, root]);

	assert.equal(
		lineageTreeNeedsHistoryRefresh(cachedNodes, [omittedChild, root], knownOmissions),
		false
	);

	const newChild = generation('new-job', 'new-asset', 'root-asset');
	assert.equal(
		lineageTreeNeedsHistoryRefresh(cachedNodes, [newChild, omittedChild, root], knownOmissions),
		true
	);

	const changedRoot = generation('root-job', 'replacement-root-asset', null);
	assert.equal(lineageTreeNeedsHistoryRefresh(cachedNodes, [changedRoot], knownOmissions), true);
});

test('persisted canvas coordinates stay inside the documented world range', () => {
	assert.equal(clampLineageCoordinate(1e300), LINEAGE_WORLD_LIMIT);
	assert.equal(clampLineageCoordinate(-1e300), -LINEAGE_WORLD_LIMIT);
});

test('restored viewport keeps its anchor at the saved screen position', () => {
	assert.deepEqual(
		rebaseLineageViewport(
			{ translateX: 120, translateY: 80, scale: 1.5 },
			{ x: 40, y: 60 },
			{ x: 100, y: 20 }
		),
		{ translateX: 30, translateY: 140 }
	);
});

test('a chain-free root is synthesised once and never fetched', () => {
	assert.equal(decideLineageTreeLoad(false, undefined), 'synthesize');
	assert.equal(decideLineageTreeLoad(false, { status: 'loaded' }), 'skip');
});

test('an in-flight or settled tree is left alone', () => {
	assert.equal(decideLineageTreeLoad(true, undefined), 'load');
	assert.equal(decideLineageTreeLoad(true, { status: 'loading' }), 'skip');
	assert.equal(decideLineageTreeLoad(true, { status: 'loaded' }), 'skip');
});

test('each failure carries its own retry, and a later failure is not starved', () => {
	// A load fails: the entry has no spent budget, so it earns one retry.
	assert.equal(decideLineageTreeLoad(true, { status: 'error' }), 'retry');
	// That retry is spent, so the same failure is not retried forever.
	assert.equal(decideLineageTreeLoad(true, { status: 'error', retried: true }), 'skip');
	// A later forced load (a derivative completing, or the user returning) replaces
	// the entry. When that one fails it is a fresh failure and earns its own retry:
	// the previous exhausted budget must not carry over.
	assert.equal(decideLineageTreeLoad(true, { status: 'loading' }), 'skip');
	assert.equal(decideLineageTreeLoad(true, { status: 'error' }), 'retry');
	assert.equal(decideLineageTreeLoad(true, { status: 'error', retried: true }), 'skip');
});

test('a persistently failing subtree retries once, not forever', () => {
	// Walks the real transitions: every load rebuilds the entry, so the budget has
	// to survive them or the automatic retry keeps re-earning one.
	type Entry = { status: 'loading' | 'loaded' | 'error'; retried?: boolean };
	let entry: Entry | undefined;
	const attempts: string[] = [];

	const attemptLoad = (force: boolean) => {
		const retained = retainedRetryBudget(force, entry);
		entry = { status: 'loading', retried: retained };
		attempts.push(force ? 'forced' : 'plain');
		entry = { status: 'error', retried: retained }; // the request fails
	};

	attemptLoad(false);
	for (let guard = 0; guard < 10; guard += 1) {
		const decision = decideLineageTreeLoad(true, entry);
		if (decision === 'skip') break;
		assert.equal(decision, 'retry');
		entry = { ...(entry as Entry), retried: true };
		attemptLoad(false);
	}

	// One initial attempt plus exactly one automatic retry.
	assert.deepEqual(attempts, ['plain', 'plain']);
	assert.equal(decideLineageTreeLoad(true, entry), 'skip');

	// A later forced load - a derivative completing, or the user returning - is a
	// new request and earns its own single retry.
	attemptLoad(true);
	assert.equal(decideLineageTreeLoad(true, entry), 'retry');
});
