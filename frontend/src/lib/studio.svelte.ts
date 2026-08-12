// Shared studio state: the sidebar (model list, gallery) and the generate
// panel look at the same registry and history.

import { t } from '$lib/i18n.svelte';
import {
	beginOptimisticStarMutation,
	clampLineageCoordinate,
	rollbackOptimisticStarMutation,
	settleStarredListMutation,
	starredListSnapshotIsCurrent
} from '$lib/lineage-canvas-state';

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
const LINEAGE_VIEWPORT_STORAGE_KEY = 'potocolom-lineage-viewport';
const LINEAGE_TREE_OFFSETS_STORAGE_KEY = 'potocolom-lineage-tree-offsets';
const LINEAGE_ROOT_ID_PATTERN =
	/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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

function loadRemovedModelIds(): string[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = localStorage.getItem(REMOVED_MODELS_STORAGE_KEY);
		const parsed = raw ? JSON.parse(raw) : [];
		return Array.isArray(parsed)
			? parsed.filter((value): value is string => typeof value === 'string')
			: [];
	} catch {
		return [];
	}
}

function loadLineageViewport(): LineageViewport | null {
	if (typeof localStorage === 'undefined') return null;
	try {
		const raw = localStorage.getItem(LINEAGE_VIEWPORT_STORAGE_KEY);
		const parsed = raw ? (JSON.parse(raw) as Partial<LineageViewport>) : null;
		if (
			parsed === null ||
			!Number.isFinite(parsed.translateX) ||
			!Number.isFinite(parsed.translateY) ||
			!Number.isFinite(parsed.scale) ||
			(parsed.scale ?? 0) <= 0
		) {
			return null;
		}
		return {
			translateX: clampLineageCoordinate(parsed.translateX as number),
			translateY: clampLineageCoordinate(parsed.translateY as number),
			scale: parsed.scale as number,
			rootId:
				typeof parsed.rootId === 'string' && LINEAGE_ROOT_ID_PATTERN.test(parsed.rootId)
					? parsed.rootId
					: null,
			anchorX:
				Number.isFinite(parsed.anchorX) && Number.isFinite(parsed.anchorY)
					? clampLineageCoordinate(parsed.anchorX as number)
					: null,
			anchorY:
				Number.isFinite(parsed.anchorX) && Number.isFinite(parsed.anchorY)
					? clampLineageCoordinate(parsed.anchorY as number)
					: null
		};
	} catch {
		return null;
	}
}

function loadLineageTreeOffsets(): Record<string, LineageTreeOffset> {
	if (typeof localStorage === 'undefined') return {};
	try {
		const raw = localStorage.getItem(LINEAGE_TREE_OFFSETS_STORAGE_KEY);
		const parsed = raw ? (JSON.parse(raw) as unknown) : {};
		if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
		return Object.fromEntries(
			Object.entries(parsed)
				.filter(
					(entry): entry is [string, LineageTreeOffset] =>
						LINEAGE_ROOT_ID_PATTERN.test(entry[0]) &&
						entry[1] !== null &&
						typeof entry[1] === 'object' &&
						Number.isFinite((entry[1] as Partial<LineageTreeOffset>).x) &&
						Number.isFinite((entry[1] as Partial<LineageTreeOffset>).y)
				)
				.map(([id, offset]) => [
					id,
					{
						x: clampLineageCoordinate(offset.x),
						y: clampLineageCoordinate(offset.y)
					}
				])
		);
	} catch {
		return {};
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
	imageToImageModelId: '',
	upscaleModelId: '',
	removedModelIds: [] as string[],
	prompt: '',
	generationPrefill: null as GenerationPrefill | null,
	lineageViewport: loadLineageViewport(),
	lineageTreeOffsets: loadLineageTreeOffsets(),
	// Canvas node selection. It lives here rather than in the canvas component
	// because leaving the Images section destroys that component, and a node
	// selected before an Upscale should still be selected on the way back.
	// Deliberately not written to storage: a reload should open a clean canvas
	// rather than an inspector pinned to whatever was picked days ago.
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
	shellView: 'generate' as
		| 'generate'
		| 'image_to_image'
		| 'upscale'
		| 'edit_image'
		| 'image_to_text'
		| 'realtime_canvas'
		| 'images'
		| 'models'
		| 'metrics',
	metricsTab: 'usage' as 'usage' | 'benchmarks'
});

export function saveLineageViewport(viewport: LineageViewport): void {
	const bounded = {
		...viewport,
		translateX: clampLineageCoordinate(viewport.translateX),
		translateY: clampLineageCoordinate(viewport.translateY),
		anchorX: viewport.anchorX === null ? null : clampLineageCoordinate(viewport.anchorX),
		anchorY: viewport.anchorY === null ? null : clampLineageCoordinate(viewport.anchorY)
	};
	studio.lineageViewport = bounded;
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(LINEAGE_VIEWPORT_STORAGE_KEY, JSON.stringify(bounded));
	} catch {
		// The viewport remains active for this tab when storage is unavailable.
	}
}

export function saveLineageTreeOffsets(offsets: Record<string, LineageTreeOffset>): void {
	const bounded = Object.fromEntries(
		Object.entries(offsets)
			.filter(([id]) => LINEAGE_ROOT_ID_PATTERN.test(id))
			.map(([id, offset]) => [
				id,
				{
					x: clampLineageCoordinate(offset.x),
					y: clampLineageCoordinate(offset.y)
				}
			])
	);
	studio.lineageTreeOffsets = bounded;
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(LINEAGE_TREE_OFFSETS_STORAGE_KEY, JSON.stringify(bounded));
	} catch {
		// The positions remain active for this tab when storage is unavailable.
	}
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

export function openPlayground(): void {
	studio.shellView = 'generate';
}

export function openService(
	view:
		| 'generate'
		| 'image_to_image'
		| 'upscale'
		| 'edit_image'
		| 'image_to_text'
		| 'realtime_canvas'
		| 'images'
		| 'models'
): void {
	studio.shellView = view;
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

function fallbackModelId(models: Model[]): string {
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
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(REMOVED_MODELS_STORAGE_KEY, JSON.stringify(studio.removedModelIds));
	} catch {
		// The preference remains active for this tab when storage is unavailable.
	}
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
	try {
		const response = await fetch('/api/v1/models');
		if (!response.ok) return;
		studio.removedModelIds = loadRemovedModelIds();
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
	const requestEpoch = ++starredListRequestEpoch;
	const mutationEpoch = starredMutationEpoch;
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
	if (
		!starredListSnapshotIsCurrent(
			requestEpoch,
			starredListRequestEpoch,
			mutationEpoch,
			starredMutationEpoch,
			pendingStarredMutations.size > 0
		)
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

async function performStarredToggle(id: string): Promise<boolean> {
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
		const response = await fetch(`/api/v1/generations/${id}/star`, {
			method: optimistic.mutation.wasStarred ? 'DELETE' : 'POST'
		});
		if (!response.ok) {
			const result = rollback();
			await settleMutation(false);
			return result;
		}
		setFavoriteNotice('save', null);
		await settleMutation(true);
		return true;
	} catch {
		const result = rollback();
		await settleMutation(false);
		return result;
	}
}

export function toggleStarred(id: string): Promise<boolean> {
	const previous = starredMutationQueues.get(id) ?? Promise.resolve(true);
	const operation = previous.catch(() => false).then(() => performStarredToggle(id));
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
