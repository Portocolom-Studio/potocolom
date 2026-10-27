// Shared studio state: the sidebar (model list, gallery) and the generate
// panel look at the same registry and history.

import { goto } from '$app/navigation';
import { resolve } from '$app/paths';
import { apiFetch } from '$lib/api';
import { runFavoriteMigration } from '$lib/favorites-migration';
import { t } from '$lib/i18n.svelte';
import {
	studioViewSearch,
	type AdminTab,
	type MetricsTab,
	type ShellView,
	type StudioTab
} from '$lib/studio-view';
import {
	beginOptimisticStarMutation,
	clampLineageCoordinate,
	rollbackOptimisticStarMutation,
	settleStarredListMutation,
	starredListSnapshotIsCurrent
} from '$lib/lineage-canvas-state';
import { readStored, removeStored, writeStored } from '$lib/safe-storage';
import {
	activateViewPrefsOwner,
	defaultViewPrefs,
	hasViewPrefs,
	normalizeViewPrefs,
	readViewPrefs,
	writeViewPrefs,
	type ImagesPreferences,
	type MetricsPreferences,
	type ViewOwner
} from '$lib/studio-view-prefs';
import { resetLineageForestSessionCache } from '$lib/lineage-forest';
import { studioCache } from '$lib/swr-cache';
import { clearGpuSamples, stopGpuSampler } from '$lib/studio-gpu-sampler';

export type Model = {
	id: string;
	name: string;
	capabilities: string[];
	min_vram_gb: number;
	default: boolean;
	// Non-empty when the model's license demands visible credit, e.g. the
	// Stability Community License "Powered by Stability AI".
	requires_attribution?: string;
	// Worker-measured realtime frame p95; the API dumps it for every model,
	// so an uncalibrated one arrives as null rather than an absent key.
	realtime_p95_ms?: number | null;
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
	download_url: string;
	width: number;
	height: number;
};

export type Generation = {
	id: string;
	model_id: string;
	source_asset_id: string | null;
	has_derivatives?: boolean;
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
	expired?: boolean;
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
	descendants_truncated: boolean;
};

export type GenerationSubtree = {
	nodes: Array<{
		parent_job_id: string | null;
		output_asset_ids: string[];
		entry: LineageEntry;
		generation: Generation;
	}>;
	truncated: boolean;
	remaining_count_lower_bound: number;
	max_depth: number;
	max_nodes: number;
};

export type GenerationPrefill = {
	mode: 'generate' | 'image_to_image' | 'upscale';
	sourceAssetId: string | null;
	prompt: string;
	modelId: string;
	params: Record<string, unknown>;
};

export type LineageViewport = {
	translateX: number;
	translateY: number;
	scale: number;
	rootId: string | null;
	anchorX: number | null;
	anchorY: number | null;
};

export type LineageTreeOffset = {
	x: number;
	y: number;
};

const HISTORY_LIMIT = 50;
const MAX_GENERATION_STREAMS = 4;
const STREAM_RECONCILE_MS = 15_000;
const STARRED_STORAGE_KEY = 'potocolom-starred';
const REMOVED_MODELS_STORAGE_KEY = 'potocolom-removed-models';
const LINEAGE_ROOT_ID_PATTERN =
	/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function loadStarredIds(): string[] {
	try {
		const raw = readStored(STARRED_STORAGE_KEY);
		const parsed = raw ? JSON.parse(raw) : [];
		return Array.isArray(parsed)
			? parsed.filter((value): value is string => typeof value === 'string')
			: [];
	} catch {
		return [];
	}
}

function loadRemovedModelIds(): string[] {
	try {
		const raw = readStored(REMOVED_MODELS_STORAGE_KEY);
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
	// False until the first models and history answers (or failures) arrive,
	// so the views can tell "still loading" from "nothing there" and show a
	// skeleton for the first.
	modelsLoaded: false,
	historyLoaded: false,
	modelId: '',
	imageToImageModelId: '',
	upscaleModelId: '',
	removedModelIds: [] as string[],
	prompt: '',
	generationPrefill: null as GenerationPrefill | null,
	lineageViewport: null as LineageViewport | null,
	lineageTreeOffsets: {} as Record<string, LineageTreeOffset>,
	// Canvas node selection lives here because leaving Images destroys its
	// component. Owner-scoped Images preferences save it, and restore reopens
	// the inspector after the selected node is found.
	lineageSelectedAssetId: null as string | null,
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
	// Mirrors the /app URL, which +page.svelte applies; change it through the
	// open* functions below so the view survives reload, Back and deep links.
	shellView: 'generate' as ShellView,
	viewOwnerEpoch: 0,
	metricsTab: 'usage' as MetricsTab,
	adminTab: 'users' as AdminTab,
	// The Images view's starred filter. Shared so the sidebar's Starred entry
	// can open Images already filtered, and show which of the two is active.
	imagesStarredOnly: false,
	// True while the realtime canvas holds a live session; the sidebar marks
	// the canvas entry with it.
	realtimeLive: false
});

let activeViewOwner: ViewOwner | null = null;
let activeViewRole: string | null = null;
let ownerEpoch = 0;
let metricsPreferences = defaultViewPrefs('metrics') as MetricsPreferences;
let imagesPreferences = defaultViewPrefs('images') as ImagesPreferences;

function sameViewOwner(left: ViewOwner | null, right: ViewOwner | null): boolean {
	return (
		left?.kind === right?.kind &&
		(left === null ||
			right === null ||
			(left.kind === 'local' || right.kind === 'local'
				? left.kind === right.kind
				: left.id === right.id))
	);
}

function clearPrivateStudioState(): void {
	stopGenerationUpdates();
	polling = false;
	pollRequested = false;
	stopGpuSampler();
	clearGpuSamples();
	generationRefreshesInFlight.clear();
	studioCache.clear();
	resetLineageForestSessionCache();
	starredListRequestEpoch += 1;
	starredMutationEpoch += 1;
	starredListDirty = false;
	pendingStarredMutations.clear();
	starredMutationQueues.clear();
	favoriteNotices.clear();
	studio.favoriteNotice = '';
	studio.starredIds = [];
	studio.starredExtras = [];
	studio.history = [];
	studio.historyRecent = [];
	studio.historyRecentFull = false;
	studio.historyHasMore = false;
	studio.historyExtended = false;
	studio.historyLoaded = false;
	studio.selectedId = null;
	studio.selectedExtra = null;
	studio.lineageSelectedAssetId = null;
	studio.generationPrefill = null;
	studio.prompt = '';
	studio.models = [];
	studio.modelsLoaded = false;
	studio.modelId = '';
	studio.imageToImageModelId = '';
	studio.upscaleModelId = '';
	studio.imagesStarredOnly = false;
	studio.realtimeLive = false;
}

function migrateLocalLineagePrefs(owner: ViewOwner): void {
	if (owner.kind !== 'local') return;
	if (!hasViewPrefs(owner, 'images')) {
		const legacyMode = readStored('potocolom-images-mode');
		if (legacyMode !== null) {
			writeViewPrefs(owner, 'images', {
				...defaultViewPrefs('images'),
				mode: legacyMode === 'grid' ? 'grid' : 'lineage'
			});
		}
	}
	const viewportKey = 'potocolom-lineage-viewport';
	const offsetsKey = 'potocolom-lineage-tree-offsets';
	if (!hasViewPrefs(owner, 'lineage-viewport')) {
		const raw = readStored(viewportKey);
		if (raw) {
			try {
				writeViewPrefs(owner, 'lineage-viewport', JSON.parse(raw));
			} catch {}
		}
	}
	if (!hasViewPrefs(owner, 'lineage-offsets')) {
		const raw = readStored(offsetsKey);
		if (raw) {
			try {
				writeViewPrefs(owner, 'lineage-offsets', JSON.parse(raw));
			} catch {}
		}
	}
}

export function activateViewOwner(owner: ViewOwner | null, role?: string | null): void {
	const nextRole = role ?? null;
	if (sameViewOwner(activeViewOwner, owner) && activeViewRole === nextRole) return;
	const changed = !sameViewOwner(activeViewOwner, owner) || activeViewRole !== nextRole;
	if (changed) {
		ownerEpoch += 1;
		studio.viewOwnerEpoch = ownerEpoch;
		clearPrivateStudioState();
	}
	activeViewOwner = owner === null ? null : { ...owner };
	activeViewRole = nextRole;
	if (owner !== null) {
		activateViewPrefsOwner(owner);
		migrateLocalLineagePrefs(owner);
	}
	metricsPreferences = readViewPrefs(owner, 'metrics');
	imagesPreferences = readViewPrefs(owner, 'images');
	studio.metricsTab = metricsPreferences.tab;
	studio.imagesStarredOnly = imagesPreferences.starredOnly;
	studio.lineageViewport = readViewPrefs(owner, 'lineage-viewport');
	studio.lineageTreeOffsets = readViewPrefs(owner, 'lineage-offsets');
}

/** Revoke the current visit at route entry, including consecutive unknown visits. */
export function resetViewOwner(): void {
	ownerEpoch += 1;
	studio.viewOwnerEpoch = ownerEpoch;
	clearPrivateStudioState();
	activeViewOwner = null;
	activeViewRole = null;
	metricsPreferences = defaultViewPrefs('metrics');
	imagesPreferences = defaultViewPrefs('images');
	studio.metricsTab = metricsPreferences.tab;
	studio.lineageViewport = null;
	studio.lineageTreeOffsets = {};
}

export function viewOwner(): ViewOwner | null {
	return activeViewOwner === null ? null : { ...activeViewOwner };
}

export function viewOwnerEpoch(): number {
	return studio.viewOwnerEpoch;
}

export function getMetricsPreferences(): MetricsPreferences {
	return { ...metricsPreferences };
}

export function saveMetricsPreferences(next: MetricsPreferences): void {
	metricsPreferences = normalizeViewPrefs('metrics', { ...metricsPreferences, ...next });
	writeViewPrefs(activeViewOwner, 'metrics', metricsPreferences);
}

export function getImagesPreferences(): ImagesPreferences {
	return structuredClone(imagesPreferences);
}

export function saveImagesPreferences(next: ImagesPreferences): void {
	imagesPreferences = normalizeViewPrefs('images', { ...imagesPreferences, ...next });
	studio.imagesStarredOnly = imagesPreferences.starredOnly;
	writeViewPrefs(activeViewOwner, 'images', imagesPreferences);
}

/** The most recent distinct prompts in the loaded history, newest first. */
export function recentPrompts(limit = 10): string[] {
	const seen = new Set<string>();
	const recent: string[] = [];
	for (const generation of studio.history) {
		const prompt = (generation.params.prompt ?? '').trim();
		if (prompt !== '' && !seen.has(prompt)) {
			seen.add(prompt);
			recent.push(prompt);
		}
		if (recent.length === limit) break;
	}
	return recent;
}

export function saveLineageViewport(viewport: LineageViewport): void {
	const bounded = {
		...viewport,
		translateX: clampLineageCoordinate(viewport.translateX),
		translateY: clampLineageCoordinate(viewport.translateY),
		anchorX: viewport.anchorX === null ? null : clampLineageCoordinate(viewport.anchorX),
		anchorY: viewport.anchorY === null ? null : clampLineageCoordinate(viewport.anchorY)
	};
	studio.lineageViewport = bounded;
	writeViewPrefs(activeViewOwner, 'lineage-viewport', bounded);
}

export function saveLineageTreeOffsets(offsets: Record<string, LineageTreeOffset>): void {
	const validEntries = Object.entries(offsets).filter(
		([id, offset]) =>
			LINEAGE_ROOT_ID_PATTERN.test(id) && Number.isFinite(offset.x) && Number.isFinite(offset.y)
	);
	const movedIds = new Set(
		validEntries
			.filter(([id, offset]) => {
				const previous = studio.lineageTreeOffsets[id];
				return previous === undefined || previous.x !== offset.x || previous.y !== offset.y;
			})
			.map(([id]) => id)
	);
	const ordered = [
		...validEntries.filter(([id]) => !movedIds.has(id)),
		...validEntries.filter(([id]) => movedIds.has(id))
	];
	const bounded = Object.fromEntries(
		ordered.slice(-1024).map(([id, offset]) => [
			id,
			{
				x: clampLineageCoordinate(offset.x),
				y: clampLineageCoordinate(offset.y)
			}
		])
	);
	studio.lineageTreeOffsets = bounded;
	writeViewPrefs(activeViewOwner, 'lineage-offsets', bounded);
}

type FavoriteNoticeKind = 'migration' | 'expired' | 'save';
const favoriteNotices = new Map<FavoriteNoticeKind, string>();
// List snapshots are valid only if neither a newer list request nor an
// optimistic mutation started while they were loading. Toggles for the same
// generation run in order so each one decides from the preceding result.
let starredListRequestEpoch = 0;
let starredMutationEpoch = 0;
let starredMutationSequence = 0;
let starredListDirty = false;
const pendingStarredMutations = new Set<number>();
const starredMutationQueues = new Map<string, Promise<boolean>>();

function setFavoriteNotice(kind: FavoriteNoticeKind, message: string | null): void {
	if (message === null) {
		favoriteNotices.delete(kind);
	} else {
		favoriteNotices.set(kind, message);
	}
	studio.favoriteNotice = [...favoriteNotices.values()].join(' ');
}

function openView(view: ShellView, tab: StudioTab = 'usage'): void {
	const search = studioViewSearch(location.search, view, tab);
	// Clicking the view already open must not stack a duplicate history entry.
	if (search === location.search) return;
	void goto(`${resolve('/app')}${search}`, { keepFocus: true, noScroll: true });
}

export function openPlayground(): void {
	openView('generate');
}

export function openService(view: Exclude<ShellView, 'metrics' | 'admin' | 'account'>): void {
	openView(view);
}

export function openMetrics(tab?: MetricsTab): void {
	openView('metrics', tab ?? studio.metricsTab);
}

export function openImages(starredOnly = false): void {
	const collectionChanged = imagesPreferences.starredOnly !== starredOnly;
	imagesPreferences = {
		...imagesPreferences,
		starredOnly,
		...(collectionChanged
			? { gridAnchor: null, gridSelectionId: null, lineageSelection: null }
			: {})
	};
	studio.imagesStarredOnly = starredOnly;
	writeViewPrefs(activeViewOwner, 'images', imagesPreferences);
	openView('images');
}

export function openAdmin(tab: AdminTab = 'users'): void {
	openView('admin', tab);
}

export function openAccount(): void {
	openView('account');
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

export function modelIsRemoved(modelId: string): boolean {
	return studio.removedModelIds.includes(modelId);
}

export function filterTextToImageModels(models: Model[]): Model[] {
	return models.filter(
		(model) => model.capabilities.includes('text_to_image') && !modelIsRemoved(model.id)
	);
}

export function filterImageToImageModels(models: Model[]): Model[] {
	return models.filter(
		(model) => model.capabilities.includes('image_to_image') && !modelIsRemoved(model.id)
	);
}

export const UPSCALE_FAST_ID = 'realesrgan-fast';
export const UPSCALE_QUALITY_ID = 'realesrgan';

export function filterUpscaleModels(models: Model[]): Model[] {
	return models.filter(
		(model) => model.capabilities.includes('upscale') && !modelIsRemoved(model.id)
	);
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
	applyModelSelections();
}

export function fallbackModelId(models: Model[]): string {
	return models.length > 0 ? (models.find((model) => model.default) ?? models[0]).id : '';
}

function applyModelSelections(): void {
	const textToImage = filterTextToImageModels(studio.models);
	if (!textToImage.some((model) => model.id === studio.modelId)) {
		studio.modelId = fallbackModelId(textToImage);
	}
	const imageToImage = filterImageToImageModels(studio.models);
	if (!imageToImage.some((model) => model.id === studio.imageToImageModelId)) {
		studio.imageToImageModelId = fallbackModelId(imageToImage);
	}
	const upscalers = filterUpscaleModels(studio.models);
	if (!upscalers.some((model) => model.id === studio.upscaleModelId)) {
		studio.upscaleModelId = defaultUpscaleModelId(studio.models);
	}
}

function saveRemovedModelIds(): void {
	writeStored(REMOVED_MODELS_STORAGE_KEY, JSON.stringify(studio.removedModelIds));
}

export function removeModel(modelId: string): void {
	if (modelIsRemoved(modelId)) return;
	studio.removedModelIds = [...studio.removedModelIds, modelId];
	saveRemovedModelIds();
	applyModelSelections();
}

export function addModel(modelId: string): void {
	studio.removedModelIds = studio.removedModelIds.filter((id) => id !== modelId);
	saveRemovedModelIds();
	applyModelSelections();
}

export async function loadModels(): Promise<void> {
	const epoch = ownerEpoch;
	try {
		const response = await fetch('/api/v1/models');
		if (!response.ok || epoch !== ownerEpoch) return;
		const models = (await response.json()) as Model[];
		if (epoch !== ownerEpoch) return;
		studio.removedModelIds = loadRemovedModelIds();
		applyModels(models);
	} catch {
		// API unreachable; studio shows the empty-model state.
	} finally {
		if (epoch === ownerEpoch) studio.modelsLoaded = true;
	}
}

export async function migrateStoredFavorites(): Promise<void> {
	const epoch = ownerEpoch;
	const stored = loadStarredIds();
	if (stored.length === 0) return;
	const { retry, missing } = await runFavoriteMigration(stored, async (id) => {
		if (epoch !== ownerEpoch) return null;
		try {
			const response = await apiFetch(`/api/v1/generations/${id}/star`, { method: 'POST' });
			return epoch === ownerEpoch ? response.status : null;
		} catch {
			return null;
		}
	});
	if (epoch !== ownerEpoch) return;
	if (retry.length === 0) removeStored(STARRED_STORAGE_KEY);
	else writeStored(STARRED_STORAGE_KEY, JSON.stringify(retry));
	const notices: string[] = [];
	if (retry.length > 0) {
		notices.push(t('app.gen.favorite_unrestored').replace('{count}', String(retry.length)));
	}
	if (missing > 0) {
		notices.push(t('app.gen.favorite_missing').replace('{count}', String(missing)));
	}
	setFavoriteNotice('migration', notices.length > 0 ? notices.join(' ') : null);
}

export async function loadHistory(): Promise<void> {
	const epoch = ownerEpoch;
	try {
		await fetchRecentHistory(epoch);
	} finally {
		if (epoch === ownerEpoch) studio.historyLoaded = true;
	}
}

async function fetchRecentHistory(epoch: number): Promise<void> {
	const response = await fetch(`/api/v1/generations?limit=${HISTORY_LIMIT}`);
	if (epoch !== ownerEpoch) return;
	if (!response.ok) {
		reconcileStarredExtras();
		return;
	}
	const page = (await response.json()) as Generation[];
	if (epoch !== ownerEpoch) return;
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
	const epoch = ownerEpoch;
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
	if (result === null || epoch !== ownerEpoch) return false;

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
	const epoch = ownerEpoch;
	studio.selectedId = id;
	if (generationById(id) !== undefined) return;
	try {
		const response = await fetch(`/api/v1/generations/${id}`);
		if (!response.ok) return;
		const generation = (await response.json()) as Generation;
		if (epoch === ownerEpoch && studio.selectedId === id) studio.selectedExtra = generation;
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
	const ownerGeneration = ownerEpoch;
	const requestEpoch = ++starredListRequestEpoch;
	const mutationEpoch = starredMutationEpoch;
	const favorites: Generation[] = [];
	let cursor: string | null = null;
	while (true) {
		const query = cursor === null ? '' : `&cursor=${cursor}`;
		const response = await fetch(`/api/v1/generations?starred=true&limit=200${query}`);
		if (!response.ok || ownerGeneration !== ownerEpoch) return;
		const page = (await response.json()) as Generation[];
		if (ownerGeneration !== ownerEpoch) return;
		favorites.push(...page);
		if (page.length < 200) break;
		const nextCursor = page.at(-1)?.id;
		if (!nextCursor || nextCursor === cursor) break;
		cursor = nextCursor;
	}
	if (
		!starredListSnapshotIsCurrent(
			requestEpoch,
			starredListRequestEpoch,
			mutationEpoch,
			starredMutationEpoch,
			pendingStarredMutations.size > 0
		) ||
		ownerGeneration !== ownerEpoch
	) {
		return;
	}
	starredListDirty = false;
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

async function performStarredToggle(id: string, ownerGeneration: number): Promise<boolean> {
	const optimistic = beginOptimisticStarMutation(studio.starredIds, id);
	const mutationToken = ++starredMutationSequence;
	pendingStarredMutations.add(mutationToken);
	starredMutationEpoch += 1;
	studio.starredIds = optimistic.starredIds;
	const rollback = (): false => {
		studio.starredIds = rollbackOptimisticStarMutation(studio.starredIds, optimistic.mutation);
		setFavoriteNotice('save', t('app.gen.favorite_save_failed'));
		return false;
	};
	const settleMutation = async (succeeded: boolean): Promise<void> => {
		if (ownerGeneration !== ownerEpoch) return;
		pendingStarredMutations.delete(mutationToken);
		const decision = settleStarredListMutation(
			starredListDirty,
			succeeded,
			pendingStarredMutations.size
		);
		starredListDirty = decision.dirty;
		if (!decision.reload) return;
		try {
			await loadStarredGenerations();
		} catch {
			// Keep the list dirty so the next history refresh retries it.
		}
	};
	try {
		const response = await apiFetch(`/api/v1/generations/${id}/star`, {
			method: optimistic.mutation.wasStarred ? 'DELETE' : 'POST'
		});
		if (ownerGeneration !== ownerEpoch) return false;
		if (!response.ok) {
			const result = rollback();
			await settleMutation(false);
			return result;
		}
		setFavoriteNotice('save', null);
		await settleMutation(true);
		return true;
	} catch {
		if (ownerGeneration !== ownerEpoch) return false;
		const result = rollback();
		await settleMutation(false);
		return result;
	}
}

export function toggleStarred(id: string): Promise<boolean> {
	const epoch = ownerEpoch;
	const previous = starredMutationQueues.get(id) ?? Promise.resolve(true);
	const operation = previous
		.catch(() => false)
		.then(() => (epoch === ownerEpoch ? performStarredToggle(id, epoch) : false));
	starredMutationQueues.set(id, operation);
	void operation.then(
		() => {
			if (starredMutationQueues.get(id) === operation) starredMutationQueues.delete(id);
		},
		() => {
			if (starredMutationQueues.get(id) === operation) starredMutationQueues.delete(id);
		}
	);
	return operation;
}

export async function cancelGeneration(id: string): Promise<boolean> {
	const epoch = ownerEpoch;
	try {
		const response = await apiFetch(`/api/v1/generations/${id}/cancel`, { method: 'POST' });
		if (!response.ok || epoch !== ownerEpoch) return false;
	} catch {
		return false;
	}
	if (epoch !== ownerEpoch) return false;
	await refreshGeneration(id);
	return epoch === ownerEpoch;
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
	const epoch = ownerEpoch;
	if (generationRefreshesInFlight.has(id)) return;
	generationRefreshesInFlight.add(id);
	try {
		const response = await fetch(`/api/v1/generations/${id}`);
		if (epoch !== ownerEpoch) return;
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
		if (epoch !== ownerEpoch) return;
		// The page may have unmounted while this was in flight.
		if (!updatesEnabled) return;
		replaceGeneration(generation);
		if (generation.state === 'succeeded' || generation.state === 'failed') {
			closeGenerationStream(id);
			pendingTerminalRefreshes.delete(id);
			terminalRefreshAttempts.delete(id);
		}
	} catch {
		if (epoch === ownerEpoch) countTerminalRefreshFailure(id);
	} finally {
		if (epoch === ownerEpoch) generationRefreshesInFlight.delete(id);
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
	const epoch = ownerEpoch;
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
		if (epoch !== ownerEpoch) return;
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
		if (epoch !== ownerEpoch) return;
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
	const epoch = ownerEpoch;
	pollRequested = true;
	if (polling) return;
	polling = true;
	let nextStreamReconcile = Date.now() + STREAM_RECONCILE_MS;
	try {
		do {
			pollRequested = false;
			while (
				updatesEnabled &&
				epoch === ownerEpoch &&
				(studio.history.some((g) => g.state === 'queued' || g.state === 'running') ||
					pendingTerminalRefreshes.size > 0)
			) {
				syncGenerationStreams();
				await new Promise((resolve) => setTimeout(resolve, 1500));
				if (!updatesEnabled || epoch !== ownerEpoch) break;
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
			epoch === ownerEpoch &&
			(pollRequested || studio.history.some((g) => g.state === 'queued' || g.state === 'running'))
		);
	} finally {
		if (epoch === ownerEpoch) {
			for (const id of generationStreams.keys()) closeGenerationStream(id);
			failedGenerationStreams.clear();
			polling = false;
			if (updatesEnabled && pollRequested) void pollWhileWorking();
		}
	}
}
