import assert from 'node:assert/strict';
import test from 'node:test';
import { LineageForest, type LineageNodeData, type LineageTree } from './lineage-forest.ts';
import { layoutLineageTree } from './lineage-layout.ts';
import type { Generation, GenerationSubtree } from './studio.svelte.ts';

function generation(id: string, assetId: string, sourceAssetId: string | null = null): Generation {
	return {
		id,
		model_id: 'test-model',
		source_asset_id: sourceAssetId,
		params: {},
		state: 'succeeded',
		progress: null,
		gpu_ms: null,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: '2026-09-26T00:00:00Z',
		dispatched_at: null,
		finished_at: '2026-09-26T00:00:01Z',
		starred_at: null,
		expired_favorite: false,
		assets: [
			{
				id: assetId,
				url: `/assets/${assetId}`,
				thumbnail_url: null,
				download_url: `/assets/${assetId}/download`,
				width: 512,
				height: 512
			}
		]
	};
}

function subtree(root: Generation, children: Generation[] = []): GenerationSubtree {
	const node = (item: Generation, parentJobId: string | null) => ({
		parent_job_id: parentJobId,
		output_asset_ids: item.assets.map((asset) => asset.id),
		entry: {
			job_id: item.id,
			asset_id: item.assets[0].id,
			action: parentJobId === null ? ('generate' as const) : ('image_to_image' as const),
			model_id: item.model_id,
			created_at: item.created_at,
			state: item.state,
			thumbnail_url: item.assets[0].thumbnail_url,
			missing: false
		},
		generation: item
	});
	return {
		nodes: [node(root, null), ...children.map((child) => node(child, root.id))],
		truncated: false,
		remaining_count_lower_bound: 0,
		max_depth: 8,
		max_nodes: 500
	};
}

function fallback(root: Generation) {
	const data: LineageNodeData = {
		output_asset_ids: [root.assets[0].id],
		entry: subtree(root).nodes[0].entry,
		generation: root
	};
	return layoutLineageTree({
		id: root.assets[0].id,
		createdAt: root.created_at,
		data,
		children: []
	});
}

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((accept, refuse) => {
		resolve = accept;
		reject = refuse;
	});
	return { promise, resolve, reject };
}

async function turns(count = 4): Promise<void> {
	for (let index = 0; index < count; index += 1) await new Promise(setImmediate);
}

function setup(
	fetchSubtree: (rootId: string, signal: AbortSignal) => Promise<GenerationSubtree>,
	initialHistory: Generation[] = [],
	cache = new Map<string, LineageTree>()
) {
	const snapshots: ReadonlyMap<string, LineageTree>[] = [];
	const loaded: Generation[] = [];
	const arrived: string[][] = [];
	let history = initialHistory;
	const forest = new LineageForest({
		fetchSubtree,
		initialHistory,
		history: () => history,
		onTreesChanged: (trees) => snapshots.push(new Map(trees)),
		onRootLoaded: (root) => loaded.push(root),
		onArrived: (ids) => arrived.push(ids),
		sessionCache: cache
	});
	return {
		forest,
		cache,
		snapshots,
		loaded,
		arrived,
		setHistory: (value: Generation[]) => (history = value)
	};
}

test('a persistent subtree failure makes one retry and stops', async () => {
	const root = { ...generation('root', 'root-asset'), has_derivatives: true };
	let attempts = 0;
	const state = setup(async () => {
		attempts += 1;
		throw new Error('offline');
	});

	state.forest.ensureVisible(root, fallback(root));
	await turns();
	assert.equal(attempts, 1);
	state.forest.ensureVisible(root, fallback(root));
	await turns();
	state.forest.ensureVisible(root, fallback(root));
	await turns();

	assert.equal(attempts, 2);
	assert.equal(state.forest.trees.get(root.id)?.status, 'error');
	assert.equal(state.forest.trees.get(root.id)?.retrySpent, true);
});

test('forced refreshes coalesce behind an in-flight load', async () => {
	const root = { ...generation('root', 'root-asset'), has_derivatives: true };
	const first = deferred<GenerationSubtree>();
	const second = deferred<GenerationSubtree>();
	const requests = [first, second];
	let attempts = 0;
	const state = setup(async () => requests[attempts++].promise);

	state.forest.ensureVisible(root, fallback(root));
	state.forest.force(root);
	state.forest.force(root);
	first.resolve(subtree(root));
	await turns();
	assert.equal(attempts, 2);

	const child = generation('child', 'child-asset', root.assets[0].id);
	second.resolve(subtree({ ...root, has_derivatives: true }, [child]));
	await turns();

	assert.equal(attempts, 2);
	assert.deepEqual(
		state.forest.trees.get(root.id)?.layout?.nodes.map((node) => node.id),
		['root-asset', 'child-asset']
	);
	assert.deepEqual(state.arrived, [['child-asset']]);
});

test('a fifth root waits until one of four active loads finishes', async () => {
	const roots = Array.from({ length: 5 }, (_, index) => ({
		...generation(`root-${index}`, `asset-${index}`),
		has_derivatives: true
	}));
	const requests = roots.map(() => deferred<GenerationSubtree>());
	const started: string[] = [];
	const state = setup(async (rootId) => {
		started.push(rootId);
		return requests[Number(rootId.slice('root-'.length))].promise;
	});

	for (const root of roots) state.forest.ensureVisible(root, fallback(root));
	await turns();
	assert.deepEqual(
		started,
		roots.slice(0, 4).map((root) => root.id)
	);

	requests[0].resolve(subtree(roots[0]));
	await turns();
	assert.deepEqual(
		started,
		roots.map((root) => root.id)
	);
	state.forest.stop();
});

test('a remount does not reuse a stale response from an interrupted forced refresh', async () => {
	const root = { ...generation('root', 'root-asset'), has_derivatives: true };
	const stale = deferred<GenerationSubtree>();
	const interrupted = deferred<GenerationSubtree>();
	const cache = new Map<string, LineageTree>();
	const first = setup(
		async () => (first.loaded.length === 0 ? stale.promise : interrupted.promise),
		[],
		cache
	);

	first.forest.ensureVisible(root, fallback(root));
	first.forest.force(root);
	stale.resolve(subtree(root));
	await turns();
	assert.equal(first.loaded.length, 1);
	first.forest.stop();
	assert.equal(cache.has(root.id), false);

	const freshChild = generation('fresh-child', 'fresh-asset', root.assets[0].id);
	let freshRequests = 0;
	const second = setup(
		async () => {
			freshRequests += 1;
			return subtree(root, [freshChild]);
		},
		[],
		cache
	);
	second.forest.ensureVisible(root, fallback(root));
	await turns();

	assert.equal(freshRequests, 1);
	assert.ok(
		second.forest.trees.get(root.id)?.layout?.nodes.some((node) => node.id === 'fresh-asset')
	);
});

test('a completed derivative invalidates and refreshes its loaded tree', async () => {
	const root = { ...generation('root', 'root-asset'), has_derivatives: true };
	const child = generation('child', 'child-asset', root.assets[0].id);
	const responses = [subtree(root), subtree(root, [child])];
	let attempts = 0;
	const state = setup(async () => responses[attempts++]);

	state.forest.ensureVisible(root, fallback(root));
	await turns();
	const roots = state.forest.reconcileFinished([root], [child], false, () => false);
	await turns();

	assert.equal(roots[0].has_derivatives, true);
	assert.equal(attempts, 2);
	assert.ok(
		state.forest.trees.get(root.id)?.layout?.nodes.some((node) => node.id === child.assets[0].id)
	);
});

test('a truncated tree ignores known omissions and refreshes for a new child', async () => {
	const root = { ...generation('root', 'root-asset'), has_derivatives: true };
	const omitted = generation('omitted', 'omitted-asset', root.assets[0].id);
	const added = generation('added', 'added-asset', root.assets[0].id);
	const first = { ...subtree(root), truncated: true, remaining_count_lower_bound: 1 };
	const responses = [first, subtree(root, [omitted, added])];
	let attempts = 0;
	const state = setup(async () => responses[attempts++]);
	state.setHistory([omitted, root]);

	state.forest.ensureVisible(root, fallback(root));
	await turns();
	state.forest.reconcileHistory([root], [omitted, root]);
	await turns();
	assert.equal(attempts, 1);

	state.setHistory([added, omitted, root]);
	state.forest.reconcileHistory([root], [added, omitted, root]);
	await turns();
	assert.equal(attempts, 2);
});

test('stop aborts requests and ignores a late response', async () => {
	const root = { ...generation('root', 'root-asset'), has_derivatives: true };
	const request = deferred<GenerationSubtree>();
	let signal: AbortSignal | undefined;
	const state = setup(async (_rootId, requestSignal) => {
		signal = requestSignal;
		return request.promise;
	});

	state.forest.ensureVisible(root, fallback(root));
	state.forest.stop();
	assert.equal(signal?.aborted, true);
	request.resolve(subtree(root));
	await turns();

	assert.equal(state.loaded.length, 0);
	assert.equal(state.cache.size, 0);
	assert.equal(state.snapshots.at(-1)?.get(root.id)?.status, 'loading');
});
