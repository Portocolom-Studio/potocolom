import assert from 'node:assert/strict';
import test from 'node:test';
import {
	LINEAGE_WORLD_LIMIT,
	clampLineageCoordinate,
	lineageTreeNeedsHistoryRefresh,
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

	assert.equal(lineageTreeNeedsHistoryRefresh(cachedNodes, [omittedChild, root], true), false);

	const changedRoot = generation('root-job', 'replacement-root-asset', null);
	assert.equal(lineageTreeNeedsHistoryRefresh(cachedNodes, [changedRoot], true), true);
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
