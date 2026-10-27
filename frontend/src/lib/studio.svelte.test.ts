import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { navigationCalls, resetNavigationCalls } from '../../scripts/test-stubs/navigation.ts';
import { LineageForest } from './lineage-forest.ts';
import {
	clearGpuSamples,
	gpuSamples,
	lastGpuHardware,
	startGpuSampler,
	stopGpuSampler
} from './studio-gpu-sampler.ts';
import { accountCacheKey, studioCache } from './swr-cache.ts';
import type { Generation, GenerationSubtree, Model } from './studio.svelte.ts';

type StudioModule = typeof import('./studio.svelte.ts');

const globals = globalThis as unknown as Record<string, unknown>;
const saved = new Map<string, unknown>();

function stub(name: string, value: unknown): void {
	if (!saved.has(name)) saved.set(name, globals[name]);
	globals[name] = value;
}

afterEach(() => {
	for (const [name, value] of saved) {
		globals[name] = value;
	}
	saved.clear();
	resetNavigationCalls();
});

let freshLoads = 0;
// Every test gets its own studio (history, starred list, poll flags, mutation
// queues), so none of them depends on the order the file runs in.
async function freshStudio(): Promise<StudioModule> {
	freshLoads += 1;
	const url = new URL(`./studio.svelte.ts?fresh=${freshLoads}`, import.meta.url).href;
	return (await import(url)) as StudioModule;
}

function storage(values: Map<string, string>) {
	return {
		getItem: (key: string) => values.get(key) ?? null,
		setItem: (key: string, value: string) => void values.set(key, value),
		removeItem: (key: string) => void values.delete(key)
	};
}

function jsonResponse(body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

async function turns(count = 8): Promise<void> {
	for (let index = 0; index < count; index += 1) await Promise.resolve();
}

function generation(id: string): Generation {
	return {
		id,
		model_id: 'model-a',
		source_asset_id: null,
		params: {},
		state: 'succeeded',
		progress: null,
		gpu_ms: null,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: '2026-01-01T00:00:00.000Z',
		dispatched_at: null,
		finished_at: null,
		starred_at: null,
		expired_favorite: false,
		assets: []
	};
}

function model(id: string, isDefault: boolean): Model {
	return {
		id,
		name: id,
		capabilities: ['text_to_image', 'upscale'],
		min_vram_gb: 8,
		default: isDefault,
		estimated_gpu_ms_default: null,
		parameters: {}
	};
}

test('loadHistory fills the studio history from the generations endpoint', async () => {
	const studio = await freshStudio();
	const requests: string[] = [];
	stub('fetch', (input: RequestInfo | URL) => {
		requests.push(String(input));
		return Promise.resolve(jsonResponse([generation('gen-1'), generation('gen-2')]));
	});

	await studio.loadHistory();

	assert.deepEqual(requests, ['/api/v1/generations?limit=50']);
	assert.deepEqual(
		studio.studio.history.map((entry) => entry.id),
		['gen-1', 'gen-2']
	);
	assert.deepEqual(
		studio.studio.historyRecent.map((entry) => entry.id),
		['gen-1', 'gen-2']
	);
	assert.equal(studio.studio.historyHasMore, false);
});

test('view preferences follow UUID owners and role changes advance the epoch', async () => {
	const values = new Map<string, string>();
	stub('localStorage', storage(values));
	const studio = await freshStudio();
	const ownerA = { kind: 'account' as const, id: '550e8400-e29b-41d4-a716-446655440000' };
	const ownerB = { kind: 'account' as const, id: '550e8400-e29b-41d4-a716-446655440001' };
	studio.activateViewOwner(ownerA, 'admin');
	const epoch = studio.viewOwnerEpoch();
	studio.saveMetricsPreferences({
		tab: 'benchmarks',
		usageRange: '24h',
		mineRange: '7d',
		selectedSessionId: '2026-10-27T12:00:00Z'
	});
	studio.activateViewOwner(ownerB, 'user');
	assert.ok(studio.viewOwnerEpoch() > epoch);
	assert.equal(studio.getMetricsPreferences().tab, 'usage');
	assert.equal(studio.getImagesPreferences().search, '');
	const ownerBEpoch = studio.viewOwnerEpoch();
	studio.activateViewOwner(ownerA, 'admin');
	assert.ok(studio.viewOwnerEpoch() > ownerBEpoch);
	assert.equal(studio.getMetricsPreferences().tab, 'benchmarks');
	assert.equal(studio.getMetricsPreferences().selectedSessionId, '2026-10-27T12:00:00Z');
	const sameOwnerEpoch = studio.viewOwnerEpoch();
	studio.activateViewOwner(ownerA, 'admin');
	assert.equal(studio.viewOwnerEpoch(), sameOwnerEpoch);
	studio.activateViewOwner(ownerA, 'user');
	assert.ok(studio.viewOwnerEpoch() > sameOwnerEpoch);
});

test('openImages resets collection resume state only when All or Starred changes', async () => {
	const values = new Map<string, string>();
	stub('localStorage', storage(values));
	stub('location', { search: '?view=images' });
	const studio = await freshStudio();
	studio.activateViewOwner(
		{ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' },
		'admin'
	);
	const allAnchor = '550e8400-e29b-41d4-a716-446655440010';
	const allAsset = '550e8400-e29b-41d4-a716-446655440011';
	studio.saveImagesPreferences({
		...studio.getImagesPreferences(),
		mode: 'grid',
		action: 'upscale',
		search: 'keep this search',
		filters: { ...studio.getImagesPreferences().filters, models: ['model-a'], range: 'last7' },
		gridAnchor: { id: allAnchor, beforeCursor: null, offset: 23 },
		gridSelectionId: allAsset,
		lineageSelection: { rootId: allAnchor, assetId: allAsset }
	});
	studio.saveLineageViewport({
		translateX: 12,
		translateY: 34,
		scale: 1.25,
		rootId: allAnchor,
		anchorX: 4,
		anchorY: 5
	});
	studio.saveLineageTreeOffsets({ [allAnchor]: { x: 56, y: 78 } });

	studio.openImages(true);
	let prefs = studio.getImagesPreferences();
	assert.equal(prefs.starredOnly, true);
	assert.equal(prefs.gridAnchor, null);
	assert.equal(prefs.gridSelectionId, null);
	assert.equal(prefs.lineageSelection, null);
	assert.equal(prefs.mode, 'grid');
	assert.equal(prefs.action, 'upscale');
	assert.equal(prefs.search, 'keep this search');
	assert.equal(prefs.filters.range, 'last7');
	assert.deepEqual(prefs.filters.models, ['model-a']);
	assert.deepEqual(studio.studio.lineageViewport, {
		translateX: 12,
		translateY: 34,
		scale: 1.25,
		rootId: allAnchor,
		anchorX: 4,
		anchorY: 5
	});
	assert.deepEqual(studio.studio.lineageTreeOffsets, { [allAnchor]: { x: 56, y: 78 } });

	const starredAnchor = '550e8400-e29b-41d4-a716-446655440020';
	const starredAsset = '550e8400-e29b-41d4-a716-446655440021';
	studio.saveImagesPreferences({
		...prefs,
		gridAnchor: { id: starredAnchor, beforeCursor: allAnchor, offset: 17 },
		gridSelectionId: starredAsset,
		lineageSelection: { rootId: starredAnchor, assetId: starredAsset }
	});
	studio.openImages(true);
	prefs = studio.getImagesPreferences();
	assert.deepEqual(prefs.gridAnchor, { id: starredAnchor, beforeCursor: allAnchor, offset: 17 });
	assert.equal(prefs.gridSelectionId, starredAsset);
	assert.deepEqual(prefs.lineageSelection, { rootId: starredAnchor, assetId: starredAsset });

	studio.openImages(false);
	prefs = studio.getImagesPreferences();
	assert.equal(prefs.starredOnly, false);
	assert.equal(prefs.gridAnchor, null);
	assert.equal(prefs.gridSelectionId, null);
	assert.equal(prefs.lineageSelection, null);
});

test('resetting an unknown owner starts a new volatile visit every time', async () => {
	const studio = await freshStudio();
	studio.activateViewOwner(null, 'user');
	const first = studio.viewOwnerEpoch();
	studio.resetViewOwner();
	assert.ok(studio.viewOwnerEpoch() > first);
	const second = studio.viewOwnerEpoch();
	studio.resetViewOwner();
	assert.ok(studio.viewOwnerEpoch() > second);
	assert.equal(studio.viewOwner(), null);
	assert.equal(studio.getMetricsPreferences().tab, 'usage');
});

test('local entry migrates legacy Images and Lineage preferences once', async () => {
	const values = new Map<string, string>([
		['potocolom-images-mode', 'grid'],
		[
			'potocolom-lineage-viewport',
			JSON.stringify({
				translateX: 12,
				translateY: -8,
				scale: 1.25,
				rootId: '550e8400-e29b-41d4-a716-446655440010',
				anchorX: 50,
				anchorY: 60
			})
		],
		[
			'potocolom-lineage-tree-offsets',
			JSON.stringify({
				'550e8400-e29b-41d4-a716-446655440010': { x: 24, y: -16 }
			})
		]
	]);
	stub('localStorage', storage(values));
	const studio = await freshStudio();
	studio.activateViewOwner({ kind: 'local' });
	assert.equal(studio.getImagesPreferences().mode, 'grid');
	assert.equal(studio.studio.lineageViewport?.translateX, 12);
	assert.equal(studio.studio.lineageTreeOffsets['550e8400-e29b-41d4-a716-446655440010']?.x, 24);
	values.set('potocolom-images-mode', 'lineage');
	studio.resetViewOwner();
	studio.activateViewOwner({ kind: 'local' });
	assert.equal(studio.getImagesPreferences().mode, 'grid');
});

test('account entry does not import local legacy Images or Lineage preferences', async () => {
	const values = new Map<string, string>([
		['potocolom-images-mode', 'grid'],
		[
			'potocolom-lineage-viewport',
			JSON.stringify({
				translateX: 12,
				translateY: -8,
				scale: 1.25,
				rootId: '550e8400-e29b-41d4-a716-446655440010',
				anchorX: 50,
				anchorY: 60
			})
		],
		[
			'potocolom-lineage-tree-offsets',
			JSON.stringify({
				'550e8400-e29b-41d4-a716-446655440010': { x: 24, y: -16 }
			})
		]
	]);
	stub('localStorage', storage(values));
	const studio = await freshStudio();
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	assert.equal(studio.getImagesPreferences().mode, 'lineage');
	assert.equal(studio.studio.lineageViewport, null);
	assert.deepEqual(studio.studio.lineageTreeOffsets, {});
});

test('owner activation clears shared report, forest, and GPU state and fences late replies', async (context) => {
	context.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
	const studio = await freshStudio();
	const ownerA = { kind: 'account' as const, id: '550e8400-e29b-41d4-a716-446655440000' };
	const ownerB = { kind: 'account' as const, id: '550e8400-e29b-41d4-a716-446655440001' };
	const root = {
		...generation('550e8400-e29b-41d4-a716-446655440010'),
		has_derivatives: true,
		assets: [
			{
				id: '550e8400-e29b-41d4-a716-446655440011',
				url: '/assets/550e8400-e29b-41d4-a716-446655440011',
				thumbnail_url: null,
				download_url: '/assets/550e8400-e29b-41d4-a716-446655440011/download',
				width: 512,
				height: 512
			}
		]
	};
	const tree: GenerationSubtree = {
		nodes: [
			{
				parent_job_id: null,
				output_asset_ids: [root.assets[0].id],
				entry: {
					job_id: root.id,
					asset_id: root.assets[0].id,
					action: 'generate',
					model_id: root.model_id,
					created_at: root.created_at,
					state: root.state,
					thumbnail_url: null,
					missing: false
				},
				generation: root
			}
		],
		truncated: false,
		remaining_count_lower_bound: 0,
		max_depth: 8,
		max_nodes: 500
	};
	let resolveOldTree!: (value: GenerationSubtree) => void;
	let treeRequests = 0;
	const oldTree = new Promise<GenerationSubtree>((resolve) => (resolveOldTree = resolve));
	const oldForest = new LineageForest({
		fetchSubtree: () => {
			treeRequests += 1;
			return treeRequests === 1 ? Promise.resolve(tree) : oldTree;
		},
		initialHistory: [root],
		history: () => [root],
		onTreesChanged: () => {},
		onRootLoaded: () => {},
		onArrived: () => {}
	});
	let gpuRequests = 0;
	const gpuSignals: AbortSignal[] = [];
	let resolveOldGpu!: (response: Response) => void;
	const oldGpu = new Promise<Response>((resolve) => (resolveOldGpu = resolve));
	stub('fetch', ((input: RequestInfo | URL, init?: RequestInit) => {
		if (String(input) !== '/api/v1/studio/gpu') return Promise.resolve(jsonResponse([]));
		gpuRequests += 1;
		if (gpuRequests === 1)
			return Promise.resolve(
				jsonResponse({ gpu: { device: 'owner-a-gpu', available: true, util_pct: 31 } })
			);
		gpuSignals.push(init?.signal as AbortSignal);
		return oldGpu;
	}) as typeof fetch);
	let newForest: LineageForest | null = null;
	let postReplyForest: LineageForest | null = null;
	try {
		studio.activateViewOwner(ownerA, 'admin');
		studioCache.write<Generation[]>(accountCacheKey(ownerA.id, 'metrics:failures'), [
			generation('private-report')
		]);
		studioCache.write(accountCacheKey(ownerA.id, 'metrics:usage-me:30d'), { total: 17 });
		assert.equal(
			studioCache.read<Generation[]>(accountCacheKey(ownerA.id, 'metrics:failures'))?.length,
			1
		);
		assert.deepEqual(
			studioCache.read<{ total: number }>(accountCacheKey(ownerA.id, 'metrics:usage-me:30d')),
			{ total: 17 }
		);

		oldForest.ensureVisible(root, {} as never);
		await turns();
		assert.equal(oldForest.trees.get(root.id)?.status, 'loaded');
		oldForest.force(root);
		await turns();
		assert.equal(treeRequests, 2, 'the old forest has a refresh in flight');

		startGpuSampler(() => [generation('gpu-running')]);
		await turns();
		assert.equal(gpuSamples().length, 1);
		assert.equal(lastGpuHardware()?.device, 'owner-a-gpu');
		context.mock.timers.tick(2000);
		assert.equal(gpuRequests, 2, 'the old owner has a second GPU request in flight');

		studio.activateViewOwner(ownerB, 'admin');
		assert.equal(studioCache.read(accountCacheKey(ownerA.id, 'metrics:failures')), undefined);
		assert.equal(studioCache.read(accountCacheKey(ownerA.id, 'metrics:usage-me:30d')), undefined);
		assert.deepEqual(gpuSamples(), []);
		assert.equal(lastGpuHardware(), null);
		assert.equal(gpuSignals[0]?.aborted, true);

		newForest = new LineageForest({
			fetchSubtree: async () => tree,
			initialHistory: [],
			history: () => [],
			onTreesChanged: () => {},
			onRootLoaded: () => {},
			onArrived: () => {}
		});
		assert.equal(newForest.trees.has(root.id), false, 'owner B starts without A cached roots');

		resolveOldTree(tree);
		resolveOldGpu(
			jsonResponse({ gpu: { device: 'late-owner-a-gpu', available: true, util_pct: 90 } })
		);
		await turns();
		assert.equal(gpuSamples().length, 0, 'the old GPU reply cannot publish after owner reset');
		assert.equal(lastGpuHardware(), null);
		postReplyForest = new LineageForest({
			fetchSubtree: async () => tree,
			initialHistory: [],
			history: () => [],
			onTreesChanged: () => {},
			onRootLoaded: () => {},
			onArrived: () => {}
		});
		assert.equal(postReplyForest.trees.has(root.id), false, 'the late A tree stays out of B cache');
	} finally {
		oldForest.stop();
		newForest?.stop();
		postReplyForest?.stop();
		studio.resetViewOwner();
		stopGpuSampler();
		clearGpuSamples();
		studioCache.clear();
	}
});

test('a late history response from one owner cannot fill the next owner history', async () => {
	const studio = await freshStudio();
	let finish!: (response: Response) => void;
	stub('fetch', () => new Promise<Response>((resolve) => (finish = resolve)));
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	const pending = studio.loadHistory();
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	finish(jsonResponse([generation('private-a')]));
	await pending;
	assert.deepEqual(studio.studio.history, []);
	assert.equal(studio.studio.historyLoaded, false);
});

test('a history response whose JSON resolves after owner change is ignored', async () => {
	const studio = await freshStudio();
	let resolveJson!: (value: Generation[]) => void;
	let jsonStarted!: () => void;
	const enteredJson = new Promise<void>((resolve) => (jsonStarted = resolve));
	const json = new Promise<Generation[]>((resolve) => (resolveJson = resolve));
	stub('fetch', () =>
		Promise.resolve({
			ok: true,
			json: () => {
				jsonStarted();
				return json;
			}
		} as Response)
	);
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	const pending = studio.loadHistory();
	await enteredJson;
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	resolveJson([generation('private-a')]);
	await pending;
	assert.deepEqual(studio.studio.history, []);
	assert.equal(studio.studio.historyLoaded, false);
});

test('a late detail response cannot select the same ID for a new owner', async () => {
	const studio = await freshStudio();
	let finishOld!: (response: Response) => void;
	let detailCalls = 0;
	const oldResponse = new Promise<Response>((resolve) => (finishOld = resolve));
	stub('fetch', (input: RequestInfo | URL) => {
		if (!String(input).includes('/api/v1/generations/shared-id'))
			return Promise.resolve(jsonResponse([]));
		detailCalls += 1;
		return detailCalls === 1 ? oldResponse : Promise.resolve(new Response(null, { status: 404 }));
	});
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	const oldDetail = studio.selectGeneration('shared-id');
	await new Promise<void>((resolve) => setImmediate(resolve));
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	await studio.selectGeneration('shared-id');
	finishOld(jsonResponse(generation('shared-id')));
	await oldDetail;
	assert.equal(studio.studio.selectedId, 'shared-id');
	assert.equal(studio.studio.selectedExtra, null);
});

test('a failed star request cannot roll its optimistic change back into a new owner', async () => {
	const studio = await freshStudio();
	let finish!: (response: Response) => void;
	stub('document', { cookie: '' });
	stub('fetch', () => new Promise<Response>((resolve) => (finish = resolve)));
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	studio.studio.starredIds = ['private-a'];
	const pending = studio.toggleStarred('private-a');
	await new Promise<void>((resolve) => setImmediate(resolve));
	assert.deepEqual(studio.studio.starredIds, []);
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	finish(new Response(null, { status: 500 }));
	assert.equal(await pending, false);
	assert.deepEqual(studio.studio.starredIds, []);
	assert.equal(studio.studio.favoriteNotice, '');
});

test('a rejected star request cannot roll an old unstar back into the new owner', async () => {
	const studio = await freshStudio();
	let rejectRequest!: (error: Error) => void;
	let requestStarted!: () => void;
	const started = new Promise<void>((resolve) => (requestStarted = resolve));
	const request = new Promise<Response>((_resolve, reject) => (rejectRequest = reject));
	stub('document', { cookie: '' });
	stub('fetch', (input: RequestInfo | URL) => {
		if (String(input).endsWith('/star')) {
			requestStarted();
			return request;
		}
		return Promise.resolve(jsonResponse([]));
	});
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	studio.studio.starredIds = ['private-a'];
	const pending = studio.toggleStarred('private-a');
	await started;
	assert.deepEqual(studio.studio.starredIds, []);
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	rejectRequest(new Error('network unavailable'));
	assert.equal(await pending, false);
	assert.deepEqual(studio.studio.starredIds, []);
	assert.equal(studio.studio.favoriteNotice, '');
});

test('an old poll cannot stop the new owner poll or close its stream', async () => {
	const studio = await freshStudio();
	const wakeups: Array<() => void> = [];
	const sources: Array<{ closed: boolean; onerror: (() => void) | null }> = [];
	class FakeEventSource {
		closed = false;
		onerror: (() => void) | null = null;
		constructor(_url: string) {
			sources.push(this);
		}
		close() {
			this.closed = true;
		}
	}
	stub('EventSource', FakeEventSource);
	stub('setTimeout', ((callback: () => void) => {
		wakeups.push(callback);
		return 1 as unknown as ReturnType<typeof setTimeout>;
	}) as typeof setTimeout);
	stub('clearTimeout', (() => {}) as typeof clearTimeout);
	stub('fetch', () => Promise.resolve(jsonResponse([])));
	const runningA = { ...generation('running-a'), state: 'running' };
	const runningB = { ...generation('running-b'), state: 'running' };
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	studio.studio.history = [runningA];
	const oldPoll = studio.startGenerationUpdates();
	await new Promise<void>((resolve) => setImmediate(resolve));
	assert.equal(sources.length, 1);
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	studio.studio.history = [runningB];
	const newPoll = studio.startGenerationUpdates();
	await new Promise<void>((resolve) => setImmediate(resolve));
	assert.equal(sources.length, 2);
	wakeups[0]();
	await oldPoll;
	assert.equal(sources[1].closed, false);
	assert.equal(wakeups.length, 2);
	studio.stopGenerationUpdates();
	wakeups[1]();
	await newPoll;
});

test('moving an old tree offset preserves it during bounded eviction', async () => {
	const values = new Map<string, string>();
	stub('localStorage', storage(values));
	const studio = await freshStudio();
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' }, 'user');
	const offsets = Object.fromEntries(
		Array.from({ length: 1024 }, (_, index) => [
			'550e8400-e29b-41d4-a716-' + String(index).padStart(12, '0'),
			{ x: index, y: -index }
		])
	);
	studio.saveLineageTreeOffsets(offsets);
	const movedOld = {
		...offsets,
		'550e8400-e29b-41d4-a716-000000000000': { x: 9999, y: 9999 }
	};
	studio.saveLineageTreeOffsets(movedOld);
	assert.equal(Object.keys(studio.studio.lineageTreeOffsets).length, 1024);
	assert.deepEqual(studio.studio.lineageTreeOffsets['550e8400-e29b-41d4-a716-000000000000'], {
		x: 9999,
		y: 9999
	});
});

test('toggleStarred moves the list before the star request and rolls it back on failure', async () => {
	const studio = await freshStudio();
	const starRequests: { url: string; init: RequestInit }[] = [];
	let optimistic: string[] = [];
	stub('document', { cookie: 'potocolom_csrf=abc123' });
	stub('fetch', (input: RequestInfo | URL, init: RequestInit = {}) => {
		const url = String(input);
		if (!url.endsWith('/star')) return Promise.resolve(jsonResponse([]));
		optimistic = [...studio.studio.starredIds];
		starRequests.push({ url, init });
		return Promise.resolve(new Response(null, { status: 500 }));
	});

	const settled = await studio.toggleStarred('gen-1');

	assert.deepEqual(optimistic, ['gen-1']);
	assert.equal(settled, false);
	assert.deepEqual(studio.studio.starredIds, []);
	assert.notEqual(studio.studio.favoriteNotice, '');
	// The star request went through apiFetch: the cookie rides along as a header
	// on an included-credentials request, which a bare fetch would not send.
	assert.equal(starRequests.length, 1);
	assert.equal(starRequests[0].url, '/api/v1/generations/gen-1/star');
	assert.equal(starRequests[0].init.credentials, 'include');
	assert.equal(new Headers(starRequests[0].init.headers).get('x-csrf-token'), 'abc123');
});

test('migrateStoredFavorites re-stars the stored favorites through apiFetch', async () => {
	const values = new Map([
		['potocolom-starred', JSON.stringify(['123e4567-e89b-42d3-a456-426614174000'])]
	]);
	const requests: { url: string; init: RequestInit }[] = [];
	stub('localStorage', storage(values));
	stub('document', { cookie: 'potocolom_csrf=abc123' });
	stub('fetch', (input: RequestInfo | URL, init: RequestInit = {}) => {
		requests.push({ url: String(input), init });
		return Promise.resolve(new Response(null, { status: 204 }));
	});

	const studio = await freshStudio();
	await studio.migrateStoredFavorites();

	assert.equal(requests.length, 1);
	assert.equal(requests[0].url, '/api/v1/generations/123e4567-e89b-42d3-a456-426614174000/star');
	assert.equal(requests[0].init.method, 'POST');
	assert.equal(requests[0].init.credentials, 'include');
	assert.equal(new Headers(requests[0].init.headers).get('x-csrf-token'), 'abc123');
	assert.equal(values.has('potocolom-starred'), false);
	assert.equal(studio.studio.favoriteNotice, '');
});

test('a delayed favorite migration cannot continue or clear legacy state after owner change', async () => {
	const favoriteIds = [
		'123e4567-e89b-42d3-a456-426614174000',
		'123e4567-e89b-42d3-a456-426614174002'
	];
	const values = new Map([['potocolom-starred', JSON.stringify(favoriteIds)]]);
	const requests: string[] = [];
	let release!: (response: Response) => void;
	let started!: () => void;
	const requestStarted = new Promise<void>((resolve) => (started = resolve));
	const pendingResponse = new Promise<Response>((resolve) => (release = resolve));
	stub('localStorage', storage(values));
	stub('document', { cookie: 'potocolom_csrf=abc123' });
	stub('fetch', (input: RequestInfo | URL) => {
		requests.push(String(input));
		started();
		return pendingResponse;
	});
	const studio = await freshStudio();
	studio.activateViewOwner(
		{ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' },
		'admin'
	);
	const pending = studio.migrateStoredFavorites();
	await requestStarted;
	studio.activateViewOwner({ kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' }, 'user');
	release(new Response(null, { status: 204 }));
	await pending;
	assert.deepEqual(requests, ['/api/v1/generations/123e4567-e89b-42d3-a456-426614174000/star']);
	assert.equal(values.has('potocolom-starred'), true);
	assert.equal(studio.studio.favoriteNotice, '');
});

test('a service view pushes history and the playground already open does not', async () => {
	const studio = await freshStudio();
	resetNavigationCalls();
	stub('location', { search: '' });

	studio.openService('upscale');
	assert.deepEqual(
		navigationCalls.map((call) => call.url),
		['/app?view=upscale']
	);
	assert.deepEqual(navigationCalls[0].options, { keepFocus: true, noScroll: true });

	studio.openPlayground();
	assert.equal(navigationCalls.length, 1);

	stub('location', { search: '?view=upscale' });
	studio.openService('upscale');
	assert.equal(navigationCalls.length, 1);
});

test('a removed model stays out of the pickers after the store reloads', async () => {
	const values = new Map<string, string>();
	stub('localStorage', storage(values));
	stub('fetch', () =>
		Promise.resolve(jsonResponse([model('model-a', true), model('model-b', false)]))
	);

	const first = await freshStudio();
	await first.loadModels();
	assert.equal(first.studio.modelId, 'model-a');
	first.removeModel('model-a');
	assert.deepEqual(JSON.parse(values.get('potocolom-removed-models') ?? '[]'), ['model-a']);

	const reloaded = await freshStudio();
	await reloaded.loadModels();

	assert.deepEqual(reloaded.studio.removedModelIds, ['model-a']);
	assert.deepEqual(
		reloaded.filterTextToImageModels(reloaded.studio.models).map((entry) => entry.id),
		['model-b']
	);
	assert.deepEqual(
		reloaded.filterUpscaleModels(reloaded.studio.models).map((entry) => entry.id),
		['model-b']
	);
	assert.equal(reloaded.studio.modelId, 'model-b');
	assert.equal(reloaded.studio.upscaleModelId, 'model-b');
});
