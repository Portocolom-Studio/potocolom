import assert from 'node:assert/strict';
import test from 'node:test';
import {
	LINEAGE_WORLD_LIMIT,
	beginOptimisticStarMutation,
	clampLineageCoordinate,
	decideInitialLineageViewportFollow,
	decideLineageLiveArrival,
	decideLineageTreeLoad,
	decideViewportScheduleAfterRootPage,
	lineageTreeOmittedHistoryJobIds,
	lineageTreeNeedsHistoryRefresh,
	shouldSpendAnchorSearchPage,
	retainedLineageTreeOffsets,
	retainedRetryBudget,
	rebaseLineageViewport,
	rollbackOptimisticStarMutation,
	settleStarredListMutation,
	settleLineageRootStarReconciliation,
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

test('viewport scheduling after a root page initialises even when the page fails', () => {
	// The contract this issue exists to fix: a failed page during the anchor
	// hunt left the viewport uninitialised because scheduling ran only on a
	// loaded page. Initialization must run whether or not the page arrived so
	// the canvas reaches the newest-tree fallback instead of stranding.
	assert.equal(decideViewportScheduleAfterRootPage(false, false, false, true), 'initialize');
	// A successful page with the viewport still not ready schedules the same way.
	assert.equal(decideViewportScheduleAfterRootPage(true, false, false, false), 'initialize');
	// A failed page on an account with no roots at all must stay unready:
	// that is what lets the successful retry initialise, instead of a wasted
	// initialization marking the viewport ready and stranding the new roots.
	assert.equal(decideViewportScheduleAfterRootPage(false, false, false, false), 'nothing');
	// A ready viewport recentres only against a page that actually arrived.
	assert.equal(decideViewportScheduleAfterRootPage(true, true, true, false), 'recenter');
	// A failed page never triggers a recentre: it would centre on stale content.
	assert.equal(decideViewportScheduleAfterRootPage(false, true, true, true), 'nothing');
	// A ready viewport with no recentre pending needs nothing.
	assert.equal(decideViewportScheduleAfterRootPage(true, true, false, true), 'nothing');
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

test('a new star goes to the front, where the reloaded list will put it', () => {
	// The API orders favorites newest-first by starred_at. Appending would place
	// the star correctly until the next reload moved it, so the optimistic order
	// has to match the server's from the start.
	assert.deepEqual(beginOptimisticStarMutation(['old', 'older'], 'fresh').starredIds, [
		'fresh',
		'old',
		'older'
	]);
	// Restoring an unstar puts the id back where it was, not at either end.
	const unstarMiddle = beginOptimisticStarMutation(['a', 'b', 'c'], 'b');
	assert.deepEqual(unstarMiddle.starredIds, ['a', 'c']);
	assert.deepEqual(rollbackOptimisticStarMutation(['a', 'c'], unstarMiddle.mutation), [
		'a',
		'b',
		'c'
	]);
});

test('starred list reload commits only at current epochs with no mutation pending', () => {
	assert.equal(starredListSnapshotIsCurrent(1, 2, 4, 4, false), false);
	assert.equal(starredListSnapshotIsCurrent(2, 2, 3, 4, false), false);
	assert.equal(starredListSnapshotIsCurrent(2, 2, 4, 4, true), false);
	assert.equal(starredListSnapshotIsCurrent(2, 2, 4, 4, false), true);
});

test('a failed later mutation reloads the starred list dirtied by an earlier success', () => {
	const afterFirstSuccess = settleStarredListMutation(false, true, 0);
	assert.deepEqual(afterFirstSuccess, { dirty: true, reload: true });
	assert.equal(starredListSnapshotIsCurrent(1, 1, 3, 4, false), false);
	assert.deepEqual(settleStarredListMutation(afterFirstSuccess.dirty, false, 0), {
		dirty: true,
		reload: true
	});
});

test('starred list reconciliation waits for the final pending mutation', () => {
	const first = settleStarredListMutation(false, true, 1);
	assert.deepEqual(first, { dirty: true, reload: false });
	assert.deepEqual(settleStarredListMutation(first.dirty, false, 0), {
		dirty: true,
		reload: true
	});
	assert.deepEqual(settleStarredListMutation(false, false, 0), {
		dirty: false,
		reload: false
	});
});

test('a root star settling after Starred roots was enabled reloads the filtered roots', () => {
	assert.deepEqual(settleLineageRootStarReconciliation({ pending: 1, dirty: false }, true, true), {
		state: { pending: 0, dirty: false },
		reload: true
	});
});

test('a later failed root toggle cannot discard an earlier successful reconciliation', () => {
	const first = settleLineageRootStarReconciliation({ pending: 2, dirty: false }, true, true);
	assert.deepEqual(first, {
		state: { pending: 1, dirty: true },
		reload: false
	});
	assert.deepEqual(settleLineageRootStarReconciliation(first.state, false, true), {
		state: { pending: 0, dirty: false },
		reload: true
	});
});

test('settled root toggles reload only when a successful change can affect the active filter', () => {
	assert.deepEqual(settleLineageRootStarReconciliation({ pending: 1, dirty: false }, true, false), {
		state: { pending: 0, dirty: false },
		reload: false
	});
	assert.deepEqual(settleLineageRootStarReconciliation({ pending: 1, dirty: false }, false, true), {
		state: { pending: 0, dirty: false },
		reload: false
	});
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

test('anchor search spends a page only when loadRoots would start work', () => {
	const ready = {
		viewportReady: false,
		rootsHaveMore: true,
		rootsFailed: false,
		rootsLoading: false,
		pagesUsed: 0,
		maxPages: 4,
		missingAnchor: true
	};
	assert.equal(shouldSpendAnchorSearchPage(ready), true);
	assert.equal(shouldSpendAnchorSearchPage({ ...ready, rootsLoading: true }), false);
	assert.equal(shouldSpendAnchorSearchPage({ ...ready, viewportReady: true }), false);
	assert.equal(shouldSpendAnchorSearchPage({ ...ready, missingAnchor: false }), false);
	assert.equal(shouldSpendAnchorSearchPage({ ...ready, rootsHaveMore: false }), false);
	assert.equal(shouldSpendAnchorSearchPage({ ...ready, rootsFailed: true }), false);
	assert.equal(shouldSpendAnchorSearchPage({ ...ready, pagesUsed: 4 }), false);
});
