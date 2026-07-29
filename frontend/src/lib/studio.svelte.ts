// Shared studio state: the sidebar (model list, gallery) and the generate
// panel look at the same registry and history.

import { t } from '$lib/i18n.svelte';

export type Model = {
	id: string;
	name: string;
	capabilities: string[];
	default: boolean;
	/** Text encoder window; absent or 0 means the model never declared one. */
	prompt_token_limit?: number;
	estimated_gpu_ms_default: number | null;
	estimated_gpu_ms_by_factor?: Record<string, number>;
	parameters: {
		properties?: Record<
			string,
			{
				type?: string;
				minimum?: number;
				maximum?: number;
				default?: number;
				enum?: number[];
			}
		>;
	} & Record<string, unknown>;
};

export type Asset = {
	id: string;
	url: string;
	thumbnail_url: string | null;
	width: number;
	height: number;
};

export type Generation = {
	id: string;
	model_id: string;
	source_asset_id: string | null;
	params: { prompt?: string } & Record<string, unknown>;
	state: string;
	progress: number | null; // denoising fraction while running, else null
	gpu_ms: number | null;
	input_fetch_ms: number | null;
	load_ms: number | null;
	postprocess_ms: number | null;
	failure_reason: string | null;
	created_at: string;
	dispatched_at: string | null;
	finished_at: string | null;
	starred_at: string | null;
	expired_favorite: boolean;
	assets: Asset[];
};

export type LineageEntry = {
	job_id: string | null;
	asset_id: string;
	action: 'generate' | 'image_to_image' | 'upscale' | 'upload';
	model_id: string | null;
	created_at: string;
	state: string | null;
	thumbnail_url: string | null;
	missing: boolean;
};

export type GenerationLineage = {
	ancestors: LineageEntry[];
	children: LineageEntry[];
	descendant_count: number;
};

const HISTORY_LIMIT = 50;
const MAX_GENERATION_STREAMS = 4;
const STREAM_RECONCILE_MS = 15_000;
const STARRED_STORAGE_KEY = 'potocolom-starred';

function loadStarredIds(): string[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = localStorage.getItem(STARRED_STORAGE_KEY);
		const parsed = raw ? JSON.parse(raw) : [];
		return Array.isArray(parsed)
			? parsed.filter((value): value is string => typeof value === 'string')
			: [];
	} catch {
		return [];
	}
}

function preserveAssetUrls(incoming: Generation[], existing: Generation[]): Generation[] {
	const byId = new Map(existing.map((generation) => [generation.id, generation]));
	return incoming.map((generation) => {
		const previous = byId.get(generation.id);
		if (!previous || generation.state !== previous.state) return generation;
		const previousByAssetId = new Map(previous.assets.map((asset) => [asset.id, asset]));
		return {
			...generation,
			assets: generation.assets.map((asset) => {
				const previousAsset = previousByAssetId.get(asset.id);
				return previousAsset
					? {
							...asset,
							url: previousAsset.url,
							thumbnail_url: previousAsset.thumbnail_url ?? asset.thumbnail_url
						}
					: asset;
			})
		};
	});
}

export const studio = $state({
	models: [] as Model[],
	modelId: '',
	prompt: '',
	selectedId: null as string | null, // generation pinned in the viewer
	selectedExtra: null as Generation | null, // selected lineage node outside loaded history
	history: [] as Generation[],
	historyRecent: [] as Generation[], // newest page; restored by "back to recent"
	historyRecentFull: false, // true when the latest API page hit the limit
	historyHasMore: false,
	historyExtended: false,
	starredIds: [] as string[],
	starredExtras: [] as Generation[], // starred jobs fetched outside the history pages
	favoriteNotice: '',
	shellView: 'playground' as 'playground' | 'metrics',
	metricsTab: 'usage' as 'usage' | 'benchmarks'
});

type FavoriteNoticeKind = 'migration' | 'expired' | 'save';
const favoriteNotices = new Map<FavoriteNoticeKind, string>();

function setFavoriteNotice(kind: FavoriteNoticeKind, message: string | null): void {
	if (message === null) {
		favoriteNotices.delete(kind);
	} else {
		favoriteNotices.set(kind, message);
	}
	studio.favoriteNotice = [...favoriteNotices.values()].join(' ');
}

export function openPlayground(): void {
	studio.shellView = 'playground';
}

export function openMetrics(tab: 'usage' | 'benchmarks' = 'usage'): void {
	studio.shellView = 'metrics';
	studio.metricsTab = tab;
}

let polling = false;
// Set when generate/upscale asks to poll while a loop is already winding down.
// Without this, `if (polling) return` drops the request in the race between the
// last idle check and `polling = false`, and the UI stops refreshing until reload.
let pollRequested = false;
let updatesEnabled = true;

type GenerationEvent = {
	job_id: string;
	state: string;
	progress?: number | null;
	reason?: string;
};

const generationStreams = new Map<string, EventSource>();
const failedGenerationStreams = new Set<string>();
const pendingTerminalRefreshes = new Set<string>();
const generationRefreshesInFlight = new Set<string>();
const terminalRefreshAttempts = new Map<string, number>();
const MAX_TERMINAL_REFRESH_ATTEMPTS = 3;

// Diffusion models drive the generate form and the sidebar picker; upscalers
// are reached only through the Upscale action (issue #91). Every model list
// the user can select from must go through this filter.
export function filterDiffusionModels(models: Model[]): Model[] {
	return models.filter((model) => !model.capabilities.includes('upscale'));
}

export const UPSCALE_FAST_ID = 'realesrgan-fast';
export const UPSCALE_QUALITY_ID = 'realesrgan';

export function filterUpscaleModels(models: Model[]): Model[] {
	return models.filter((model) => model.capabilities.includes('upscale'));
}

/** Prefer fast when registered; else quality; else first sorted id. */
export function defaultUpscaleModelId(models: Model[]): string {
	const upscalers = filterUpscaleModels(models);
	if (upscalers.length === 0) return '';
	if (upscalers.some((model) => model.id === UPSCALE_FAST_ID)) return UPSCALE_FAST_ID;
	if (upscalers.some((model) => model.id === UPSCALE_QUALITY_ID)) return UPSCALE_QUALITY_ID;
	return [...upscalers].sort((a, b) => a.id.localeCompare(b.id))[0].id;
}

function applyModels(models: Model[]): void {
	studio.models = models;
	const selectable = filterDiffusionModels(models);
	if (!studio.modelId || !selectable.some((model) => model.id === studio.modelId)) {
		studio.modelId =
			selectable.length > 0 ? (selectable.find((model) => model.default) ?? selectable[0]).id : '';
	}
}

export async function loadModels(): Promise<void> {
	try {
		const response = await fetch('/api/v1/models');
		if (!response.ok) return;
		applyModels((await response.json()) as Model[]);
	} catch {
		// API unreachable; studio shows the empty-model state.
	}
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function migrateStoredFavorites(): Promise<void> {
	const stored = loadStarredIds();
	if (stored.length === 0 || typeof localStorage === 'undefined') return;
	// A value that was never a job id can never resolve, so count it as missing
	// rather than letting its 422 hold the migration open forever.
	let missing = stored.filter((id) => !UUID_PATTERN.test(id)).length;
	let complete = true;
	for (const id of stored.filter((value) => UUID_PATTERN.test(value))) {
		try {
			// Star directly: the endpoint's own 404 already reports an id that no
			// longer resolves, so a preceding GET would only double the requests.
			const starred = await fetch(`/api/v1/generations/${id}/star`, { method: 'POST' });
			if (starred.status === 404) {
				missing += 1;
			} else if (!starred.ok) {
				complete = false;
			}
		} catch {
			complete = false;
		}
	}
	if (complete) localStorage.removeItem(STARRED_STORAGE_KEY);
	if (missing > 0) {
		setFavoriteNotice(
			'migration',
			t('app.gen.favorite_missing').replace('{count}', String(missing))
		);
	}
}

export async function loadHistory(): Promise<void> {
	const response = await fetch(`/api/v1/generations?limit=${HISTORY_LIMIT}`);
	if (!response.ok) {
		reconcileStarredExtras();
		return;
	}
	const page = (await response.json()) as Generation[];
	const recentFull = page.length === HISTORY_LIMIT;
	const recent = preserveAssetUrls(page, studio.history);
	studio.historyRecent = recent;
	studio.historyRecentFull = recentFull;
	if (!studio.historyExtended) {
		studio.history = recent;
		studio.historyHasMore = recentFull;
		reconcileStarredExtras();
		return;
	}
	// Keep older pages at the tail while refreshing the newest slice in place.
	const recentIds = new Set(recent.map((generation) => generation.id));
	const olderTail = studio.history.filter((generation) => !recentIds.has(generation.id));
	studio.history = [...recent, ...olderTail];
	reconcileStarredExtras();
}

export async function loadOlderHistory(): Promise<boolean> {
	if (!studio.historyHasMore) return false;
	const oldest = studio.history.at(-1);
	if (!oldest) return false;

	const existing = new Set(studio.history.map((generation) => generation.id));
	const fetchPage = async (query: string) => {
		const response = await fetch(`/api/v1/generations?limit=${HISTORY_LIMIT}&${query}`);
		if (!response.ok) return null;
		const raw = (await response.json()) as Generation[];
		return { raw, items: raw };
	};

	const result = await fetchPage(`cursor=${oldest.id}`);
	if (result === null) return false;

	const unique = result.items.filter((generation) => !existing.has(generation.id));

	if (unique.length === 0) {
		studio.historyHasMore = false;
		return false;
	}

	studio.history = [...studio.history, ...unique];
	studio.historyHasMore = result.raw.length === HISTORY_LIMIT;
	studio.historyExtended = studio.history.length > studio.historyRecent.length;
	reconcileStarredExtras();
	return true;
}

export function generationById(id: string): Generation | undefined {
	return (
		studio.history.find((generation) => generation.id === id) ??
		studio.starredExtras.find((generation) => generation.id === id) ??
		(studio.selectedExtra?.id === id ? studio.selectedExtra : undefined)
	);
}

export async function selectGeneration(id: string): Promise<void> {
	studio.selectedId = id;
	if (generationById(id) !== undefined) return;
	try {
		const response = await fetch(`/api/v1/generations/${id}`);
		if (!response.ok) return;
		const generation = (await response.json()) as Generation;
		if (studio.selectedId === id) studio.selectedExtra = generation;
	} catch {
		// Keep the current detail visible when an older lineage node cannot load.
	}
}

function replaceGeneration(incoming: Generation): void {
	const replace = (generations: Generation[]) =>
		generations.map((generation) =>
			generation.id === incoming.id ? preserveAssetUrls([incoming], [generation])[0] : generation
		);
	studio.history = replace(studio.history);
	studio.historyRecent = replace(studio.historyRecent);
	studio.starredExtras = replace(studio.starredExtras);
}

function applyGenerationEvent(event: GenerationEvent): void {
	const apply = (generations: Generation[]) =>
		generations.map((generation) => {
			if (generation.id !== event.job_id) return generation;
			return {
				...generation,
				state: event.state,
				progress: event.state === 'running' ? (event.progress ?? generation.progress) : null,
				failure_reason:
					event.state === 'failed' ? (event.reason ?? generation.failure_reason) : null
			};
		});
	studio.history = apply(studio.history);
	studio.historyRecent = apply(studio.historyRecent);
	studio.starredExtras = apply(studio.starredExtras);
}

export function starredGenerations(): Generation[] {
	return studio.starredIds.flatMap((id) => {
		const generation = generationById(id);
		return generation !== undefined && generation.assets.length > 0 ? [generation] : [];
	});
}

// History refreshes cannot change which generations are starred, only whether a
// favorite is already on the page. Reconciling locally keeps fallback history
// refreshes from re-paginating the whole favorites list.
export function reconcileStarredExtras(): void {
	const historyIds = new Set(studio.history.map((generation) => generation.id));
	studio.starredExtras = studio.starredExtras.filter(
		(generation) => !historyIds.has(generation.id)
	);
}

export async function loadStarredGenerations(): Promise<void> {
	const favorites: Generation[] = [];
	let cursor: string | null = null;
	while (true) {
		const query = cursor === null ? '' : `&cursor=${cursor}`;
		const response = await fetch(`/api/v1/generations?starred=true&limit=200${query}`);
		if (!response.ok) return;
		const page = (await response.json()) as Generation[];
		favorites.push(...page);
		if (page.length < 200) break;
		const nextCursor = page.at(-1)?.id;
		if (!nextCursor || nextCursor === cursor) break;
		cursor = nextCursor;
	}
	studio.starredIds = favorites.map((generation) => generation.id);
	const historyIds = new Set(studio.history.map((generation) => generation.id));
	studio.starredExtras = favorites.filter(
		(generation) => !historyIds.has(generation.id) && generation.assets.length > 0
	);
	if (favorites.some((generation) => generation.expired_favorite)) {
		setFavoriteNotice('expired', t('app.gen.favorite_expired'));
	} else {
		setFavoriteNotice('expired', null);
	}
}

export async function resetHistoryToRecent(): Promise<void> {
	studio.history = studio.historyRecent;
	studio.historyExtended = false;
	studio.historyHasMore = studio.historyRecentFull;
	await loadStarredGenerations();
}

export function isStarred(id: string): boolean {
	return studio.starredIds.includes(id);
}

export function toggleStarred(id: string): void {
	const wasStarred = isStarred(id);
	// Restoring the snapshot keeps a failed toggle from reordering the list, which
	// rebuilding it through a Set would do.
	const previous = studio.starredIds;
	const rollback = () => {
		studio.starredIds = previous;
		setFavoriteNotice('save', t('app.gen.favorite_save_failed'));
	};
	// The API orders favorites newest-first by starred_at, so a new star belongs at
	// the front; appending would make it jump once the list reloads.
	studio.starredIds = wasStarred
		? studio.starredIds.filter((starredId) => starredId !== id)
		: [id, ...studio.starredIds];
	void fetch(`/api/v1/generations/${id}/star`, {
		method: wasStarred ? 'DELETE' : 'POST'
	})
		.then(async (response) => {
			if (!response.ok) {
				rollback();
				return;
			}
			setFavoriteNotice('save', null);
			try {
				await loadStarredGenerations();
			} catch {
				// The save succeeded; the next history refresh retries the list.
			}
		})
		.catch(() => {
			rollback();
		});
}

function closeGenerationStream(id: string): void {
	generationStreams.get(id)?.close();
	generationStreams.delete(id);
}

function abandonTerminalRefresh(id: string): void {
	// Give up rather than leave the entry pending: the loop keeps running while
	// anything is pending, so a refresh that can never succeed would poll forever.
	closeGenerationStream(id);
	pendingTerminalRefreshes.delete(id);
	terminalRefreshAttempts.delete(id);
}

async function refreshGeneration(id: string): Promise<void> {
	if (generationRefreshesInFlight.has(id)) return;
	generationRefreshesInFlight.add(id);
	try {
		const response = await fetch(`/api/v1/generations/${id}`);
		if (response.status === 404) {
			// The generation is gone; retrying cannot change that.
			abandonTerminalRefresh(id);
			return;
		}
		if (!response.ok) {
			countTerminalRefreshFailure(id);
			return;
		}
		const generation = (await response.json()) as Generation;
		// The page may have unmounted while this was in flight.
		if (!updatesEnabled) return;
		replaceGeneration(generation);
		if (generation.state === 'succeeded' || generation.state === 'failed') {
			closeGenerationStream(id);
			pendingTerminalRefreshes.delete(id);
			terminalRefreshAttempts.delete(id);
		}
	} catch {
		countTerminalRefreshFailure(id);
	} finally {
		generationRefreshesInFlight.delete(id);
	}
}

function countTerminalRefreshFailure(id: string): void {
	if (!pendingTerminalRefreshes.has(id)) return;
	const attempts = (terminalRefreshAttempts.get(id) ?? 0) + 1;
	if (attempts >= MAX_TERMINAL_REFRESH_ATTEMPTS) {
		// The next full history refresh reconciles this row.
		abandonTerminalRefresh(id);
		return;
	}
	terminalRefreshAttempts.set(id, attempts);
}

function subscribeToGeneration(id: string): void {
	if (typeof EventSource === 'undefined') {
		failedGenerationStreams.add(id);
		return;
	}
	let source: EventSource;
	try {
		source = new EventSource(`/api/v1/generations/${id}/events`);
	} catch {
		failedGenerationStreams.add(id);
		return;
	}
	generationStreams.set(id, source);
	source.onmessage = (message) => {
		let event: GenerationEvent;
		try {
			event = JSON.parse(message.data) as GenerationEvent;
		} catch {
			return;
		}
		if (event.job_id !== id) return;
		applyGenerationEvent(event);
		if (event.state === 'succeeded' || event.state === 'failed') {
			closeGenerationStream(id);
			pendingTerminalRefreshes.add(id);
			void refreshGeneration(id);
		}
	};
	source.onerror = () => {
		if (generationStreams.get(id) !== source) return;
		// This covers an error before the initial snapshot as well as a broken
		// established stream. Polling is safer than relying on a reconnect that
		// may have missed the terminal event.
		closeGenerationStream(id);
		failedGenerationStreams.add(id);
		pollRequested = true;
	};
}

function syncGenerationStreams(): boolean {
	const working = studio.history.filter(
		(generation) => generation.state === 'queued' || generation.state === 'running'
	);
	const workingIds = new Set(working.map((generation) => generation.id));
	for (const id of generationStreams.keys()) {
		if (!workingIds.has(id)) closeGenerationStream(id);
	}
	for (const id of failedGenerationStreams) {
		if (!workingIds.has(id)) failedGenerationStreams.delete(id);
	}
	for (const generation of working) {
		if (generationStreams.size >= MAX_GENERATION_STREAMS) break;
		if (!generationStreams.has(generation.id) && !failedGenerationStreams.has(generation.id)) {
			subscribeToGeneration(generation.id);
		}
	}
	return working.some((generation) => !generationStreams.has(generation.id));
}

export function stopGenerationUpdates(): void {
	updatesEnabled = false;
	pollRequested = false;
	for (const id of generationStreams.keys()) closeGenerationStream(id);
	failedGenerationStreams.clear();
	pendingTerminalRefreshes.clear();
	terminalRefreshAttempts.clear();
}

/** Enable updates for a mounted view. Only the route that mounts should call this. */
export async function startGenerationUpdates(): Promise<void> {
	updatesEnabled = true;
	await pollWhileWorking();
}

export async function pollWhileWorking(): Promise<void> {
	// Requesting a tick must not re-enable a stopped view: callers reach here from
	// async work that can resolve after teardown.
	if (!updatesEnabled) return;
	pollRequested = true;
	if (polling) return;
	polling = true;
	let nextStreamReconcile = Date.now() + STREAM_RECONCILE_MS;
	try {
		do {
			pollRequested = false;
			while (
				updatesEnabled &&
				(studio.history.some((g) => g.state === 'queued' || g.state === 'running') ||
					pendingTerminalRefreshes.size > 0)
			) {
				syncGenerationStreams();
				await new Promise((resolve) => setTimeout(resolve, 1500));
				if (!updatesEnabled) break;
				if (syncGenerationStreams()) {
					try {
						await loadHistory();
						nextStreamReconcile = Date.now() + STREAM_RECONCILE_MS;
					} catch {
						// Keep the fallback active until history answers again.
					}
				} else if (Date.now() >= nextStreamReconcile) {
					await Promise.all([...generationStreams.keys()].map((id) => refreshGeneration(id)));
					nextStreamReconcile = Date.now() + STREAM_RECONCILE_MS;
				}
				await Promise.all([...pendingTerminalRefreshes].map((id) => refreshGeneration(id)));
			}
			// Re-check: a generate() during the idle gap sets pollRequested and may
			// have already refreshed history with new queued/running jobs.
		} while (
			updatesEnabled &&
			(pollRequested || studio.history.some((g) => g.state === 'queued' || g.state === 'running'))
		);
	} finally {
		for (const id of generationStreams.keys()) closeGenerationStream(id);
		failedGenerationStreams.clear();
		polling = false;
		if (updatesEnabled && pollRequested) void pollWhileWorking();
	}
}
