import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { navigationCalls, resetNavigationCalls } from '../../scripts/test-stubs/navigation.ts';
import type { Generation, Model } from './studio.svelte.ts';

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
