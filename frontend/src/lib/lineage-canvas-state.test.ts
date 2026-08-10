import assert from 'node:assert/strict';
import test from 'node:test';
import {
	LINEAGE_WORLD_LIMIT,
	beginOptimisticStarMutation,
	clampLineageCoordinate,
	decideInitialLineageViewportFollow,
	decideLineageLiveArrival,
	decideLineageTreeLoad,
	lineageTreeOmittedHistoryJobIds,
	lineageTreeNeedsHistoryRefresh,
	retainedLineageTreeOffsets,
	retainedRetryBudget,
	rebaseLineageViewport,
	rollbackOptimisticStarMutation,
	shouldReloadLineageRootsAfterStarToggle,
	shouldDimLineageEdge,
	starredListSnapshotIsCurrent
} from './lineage-canvas-state.ts';
import { layoutLineageTree, packLineageForest } from './lineage-layout.ts';
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

test('initial viewport keeps its centered root through a second root page', () => {
	const tree = (index: number, createdAt: string) => {
		const id = `root-${index}`;
		return {
			rootId: id,
			createdAt,
			hasDerivatives: false,
			layout: layoutLineageTree({ id, createdAt, data: null, children: [] })
		};
	};
	const firstPage = Array.from({ length: 50 }, (_, index) =>
		tree(index, `2026-08-09T00:00:${String(49 - index).padStart(2, '0')}Z`)
	);
	const secondPage = Array.from({ length: 51 }, (_, index) =>
		tree(index + 50, `2026-08-10T00:00:${String(50 - index).padStart(2, '0')}Z`)
	);
	const centeredRootId = firstPage[0].rootId;
	const rootPosition = (forest: ReturnType<typeof packLineageForest<null>>) => {
		const packed = forest.find((item) => item.rootId === centeredRootId);
		const root = packed?.layout.nodes.find((node) => node.id === centeredRootId);
		assert.ok(packed && root);
		return { x: packed.x + root.x, y: packed.y + root.y };
	};
	const firstAnchor = rootPosition(packLineageForest(firstPage));
	const viewport = {
		translateX: 700 - firstAnchor.x,
		translateY: 400 - firstAnchor.y,
		scale: 1
	};
	const currentAnchor = rootPosition(packLineageForest([...firstPage, ...secondPage]));

	assert.notDeepEqual(currentAnchor, firstAnchor);
	const decision = decideInitialLineageViewportFollow(
		viewport,
		{ rootId: centeredRootId, ...firstAnchor },
		currentAnchor,
		'loading'
	);
	assert.deepEqual(
		{ x: currentAnchor.x + decision.translateX, y: currentAnchor.y + decision.translateY },
		{ x: 700, y: 400 }
	);
	assert.deepEqual(decision.anchor, { rootId: centeredRootId, ...currentAnchor });

	const settled = decideInitialLineageViewportFollow(
		{ ...viewport, translateX: decision.translateX, translateY: decision.translateY },
		decision.anchor,
		currentAnchor,
		'settled'
	);
	assert.deepEqual(
		{ x: currentAnchor.x + settled.translateX, y: currentAnchor.y + settled.translateY },
		{ x: 700, y: 400 }
	);
	assert.equal(settled.anchor, null);
});

test('filter reload follows its recentered root through later root pages', () => {
	const tree = (index: number, createdAt: string) => {
		const id = `filter-root-${index}`;
		return {
			rootId: id,
			createdAt,
			hasDerivatives: false,
			layout: layoutLineageTree({ id, createdAt, data: null, children: [] })
		};
	};
	const firstPage = Array.from({ length: 50 }, (_, index) =>
		tree(index, `2026-08-09T00:00:${String(49 - index).padStart(2, '0')}Z`)
	);
	const secondPage = Array.from({ length: 50 }, (_, index) =>
		tree(index + 50, `2026-08-10T00:00:${String(49 - index).padStart(2, '0')}Z`)
	);
	const terminalPage = Array.from({ length: 49 }, (_, index) =>
		tree(index + 100, `2026-08-11T00:00:${String(48 - index).padStart(2, '0')}Z`)
	);
	const recenteredRootId = firstPage[0].rootId;
	const rootPosition = (forest: ReturnType<typeof packLineageForest<null>>) => {
		const packed = forest.find((item) => item.rootId === recenteredRootId);
		const root = packed?.layout.nodes.find((node) => node.id === recenteredRootId);
		assert.ok(packed && root);
		return { x: packed.x + root.x, y: packed.y + root.y };
	};
	const recenteredAnchor = rootPosition(packLineageForest(firstPage));
	const viewport = {
		translateX: 700 - recenteredAnchor.x,
		translateY: 400 - recenteredAnchor.y,
		scale: 1
	};
	const secondAnchor = rootPosition(packLineageForest([...firstPage, ...secondPage]));

	assert.notDeepEqual(secondAnchor, recenteredAnchor);
	const secondDecision = decideInitialLineageViewportFollow(
		viewport,
		{ rootId: recenteredRootId, ...recenteredAnchor },
		secondAnchor,
		'loading'
	);
	assert.deepEqual(secondDecision.anchor, { rootId: recenteredRootId, ...secondAnchor });
	assert.ok(secondDecision.anchor);

	const terminalAnchor = rootPosition(
		packLineageForest([...firstPage, ...secondPage, ...terminalPage])
	);
	assert.notDeepEqual(terminalAnchor, secondAnchor);
	const terminalDecision = decideInitialLineageViewportFollow(
		{ ...viewport, translateX: secondDecision.translateX, translateY: secondDecision.translateY },
		secondDecision.anchor,
		terminalAnchor,
		'settled'
	);
	assert.deepEqual(
		{
			x: terminalAnchor.x + terminalDecision.translateX,
			y: terminalAnchor.y + terminalDecision.translateY
		},
		{ x: 700, y: 400 }
	);
	assert.equal(terminalDecision.anchor, null);
});

test('failed root page keeps the viewport anchor through a successful retry', () => {
	const viewport = { translateX: 300, translateY: 200, scale: 1 };
	const storedAnchor = { rootId: 'retry-root', x: 100, y: 120 };
	const failed = decideInitialLineageViewportFollow(
		viewport,
		storedAnchor,
		{ x: 100, y: 120 },
		'failed'
	);
	assert.deepEqual(failed.anchor, storedAnchor);
	assert.equal(failed.fallbackToNewest, false);
	assert.ok(failed.anchor);

	const retried = decideInitialLineageViewportFollow(
		viewport,
		failed.anchor,
		{ x: 420, y: 360 },
		'loading'
	);
	assert.deepEqual(retried.anchor, { rootId: 'retry-root', x: 420, y: 360 });
	assert.ok(retried.anchor);

	const settled = decideInitialLineageViewportFollow(
		{ ...viewport, translateX: retried.translateX, translateY: retried.translateY },
		retried.anchor,
		{ x: 420, y: 360 },
		'settled'
	);
	assert.equal(settled.anchor, null);
	assert.equal(settled.fallbackToNewest, false);
});

test('expired viewport anchor falls back after root paging settles', () => {
	const decision = decideInitialLineageViewportFollow(
		{ translateX: 300, translateY: 200, scale: 1 },
		{ rootId: 'expired-root', x: 100, y: 120 },
		null,
		'settled'
	);

	assert.equal(decision.anchor, null);
	assert.equal(decision.fallbackToNewest, true);
});

test('filtered root loads retain offsets for roots hidden by the filter', () => {
	const offsets = {
		'starred-root': { x: 120, y: 80 },
		'unstarred-root': { x: -240, y: 160 }
	};
	const visibleRootIds = new Set(['starred-root']);

	assert.strictEqual(retainedLineageTreeOffsets(offsets, visibleRootIds, true), offsets);
	assert.deepEqual(retainedLineageTreeOffsets(offsets, visibleRootIds, false), {
		'starred-root': { x: 120, y: 80 }
	});
});

test('starred filter rejects unstarred live roots but still inspects descendants', () => {
	assert.equal(decideLineageLiveArrival(true, true, false), 'ignore');
	assert.equal(decideLineageLiveArrival(true, true, true), 'insert-root');
	assert.equal(decideLineageLiveArrival(true, false, false), 'insert-root');
	assert.equal(decideLineageLiveArrival(false, true, false), 'inspect-descendant');
});

test('failed star mutation rolls back only its generation', () => {
	const unstarA = beginOptimisticStarMutation(['a'], 'a');
	assert.deepEqual(unstarA.starredIds, []);
	const starB = beginOptimisticStarMutation(unstarA.starredIds, 'b');
	assert.deepEqual(starB.starredIds, ['b']);

	assert.deepEqual(rollbackOptimisticStarMutation(['a', 'b'], unstarA.mutation), ['a', 'b']);
	assert.deepEqual(rollbackOptimisticStarMutation(['b'], unstarA.mutation), ['a', 'b']);
	assert.deepEqual(rollbackOptimisticStarMutation(['b', 'c'], starB.mutation), ['c']);
});

test('starred list reload commits only at current request and mutation epochs', () => {
	assert.equal(starredListSnapshotIsCurrent(1, 2, 4, 4), false);
	assert.equal(starredListSnapshotIsCurrent(2, 2, 3, 4), false);
	assert.equal(starredListSnapshotIsCurrent(2, 2, 4, 4), true);
});

test('star toggle reloads roots only in the unchanged starred filter epoch', () => {
	assert.equal(shouldReloadLineageRootsAfterStarToggle(true, true, 4, 4, true), true);
	assert.equal(shouldReloadLineageRootsAfterStarToggle(true, true, 4, 5, false), false);
	assert.equal(shouldReloadLineageRootsAfterStarToggle(true, true, 4, 6, true), false);
	assert.equal(shouldReloadLineageRootsAfterStarToggle(false, true, 4, 4, true), false);
	assert.equal(shouldReloadLineageRootsAfterStarToggle(true, false, 4, 4, true), false);
});

test('edges are not dimmed when the selected node left the filtered forest', () => {
	assert.equal(shouldDimLineageEdge(false, false), false);
	assert.equal(shouldDimLineageEdge(true, false), true);
	assert.equal(shouldDimLineageEdge(true, true), false);
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
