// Shared studio state: the sidebar (model list, gallery) and the generate
// panel look at the same registry and history.

export type Model = {
	id: string;
	name: string;
	capabilities: string[];
	default: boolean;
	estimated_gpu_ms_default: number | null;
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

export type Asset = { id: string; url: string; width: number; height: number };

export type Generation = {
	id: string;
	model_id: string;
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
	assets: Asset[];
};

const HISTORY_LIMIT = 50;
const STARRED_STORAGE_KEY = 'potocolom-starred';

function loadStarredIds(): string[] {
	if (typeof localStorage === 'undefined') return [];
	try {
		const raw = localStorage.getItem(STARRED_STORAGE_KEY);
		return raw ? (JSON.parse(raw) as string[]) : [];
	} catch {
		return [];
	}
}

function saveStarredIds(ids: string[]): void {
	if (typeof localStorage === 'undefined') return;
	localStorage.setItem(STARRED_STORAGE_KEY, JSON.stringify(ids));
}

function preserveAssetUrls(incoming: Generation[], existing: Generation[]): Generation[] {
	const byId = new Map(existing.map((generation) => [generation.id, generation]));
	return incoming.map((generation) => {
		const previous = byId.get(generation.id);
		if (!previous || generation.state !== previous.state) return generation;
		const urlByAssetId = new Map(previous.assets.map((asset) => [asset.id, asset.url]));
		return {
			...generation,
			assets: generation.assets.map((asset) => {
				const url = urlByAssetId.get(asset.id);
				return url ? { ...asset, url } : asset;
			})
		};
	});
}

export const studio = $state({
	models: [] as Model[],
	modelId: '',
	prompt: '',
	selectedId: null as string | null, // generation pinned in the viewer
	history: [] as Generation[],
	historyRecent: [] as Generation[], // newest page; restored by "back to recent"
	historyRecentFull: false, // true when the latest API page hit the limit
	historyHasMore: false,
	historyExtended: false,
	starredIds: loadStarredIds() as string[],
	starredExtras: [] as Generation[], // starred jobs fetched outside the history pages
	shellView: 'playground' as 'playground' | 'metrics',
	metricsTab: 'usage' as 'usage' | 'benchmarks'
});

export function openPlayground(): void {
	studio.shellView = 'playground';
}

export function openMetrics(tab: 'usage' | 'benchmarks' = 'usage'): void {
	studio.shellView = 'metrics';
	studio.metricsTab = tab;
}

let polling = false;

// Diffusion models drive the generate form and the sidebar picker; upscalers
// are reached only through the Upscale action (issue #91). Every model list
// the user can select from must go through this filter.
export function filterDiffusionModels(models: Model[]): Model[] {
	return models.filter((model) => !model.capabilities.includes('upscale'));
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

export function syncStarredIdsFromStorage(): void {
	studio.starredIds = loadStarredIds();
}

export async function loadHistory(): Promise<void> {
	const response = await fetch(`/api/v1/generations?limit=${HISTORY_LIMIT}`);
	if (!response.ok) {
		await loadStarredGenerations();
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
		await loadStarredGenerations();
		return;
	}
	// Keep older pages at the tail while refreshing the newest slice in place.
	const recentIds = new Set(recent.map((generation) => generation.id));
	const olderTail = studio.history.filter((generation) => !recentIds.has(generation.id));
	studio.history = [...recent, ...olderTail];
	await loadStarredGenerations();
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
	await loadStarredGenerations();
	return true;
}

export function generationById(id: string): Generation | undefined {
	return (
		studio.history.find((generation) => generation.id === id) ??
		studio.starredExtras.find((generation) => generation.id === id)
	);
}

export function starredGenerations(): Generation[] {
	return studio.starredIds.flatMap((id) => {
		const generation = generationById(id);
		return generation !== undefined && generation.assets.length > 0 ? [generation] : [];
	});
}

export async function loadStarredGenerations(): Promise<void> {
	const historyIds = new Set(studio.history.map((generation) => generation.id));
	const extrasById = new Map(studio.starredExtras.map((generation) => [generation.id, generation]));

	const resolved = new Map<string, Generation>();
	for (const id of studio.starredIds) {
		const fromHistory = studio.history.find((generation) => generation.id === id);
		if (fromHistory !== undefined && fromHistory.assets.length > 0) {
			resolved.set(id, fromHistory);
			continue;
		}
		const cached = extrasById.get(id);
		if (cached !== undefined && cached.assets.length > 0) {
			resolved.set(id, cached);
		}
	}

	const missing = studio.starredIds.filter((id) => !resolved.has(id));
	const fetched =
		missing.length === 0
			? []
			: (
					await Promise.all(
						missing.map(async (id) => {
							const response = await fetch(`/api/v1/generations/${id}`);
							if (!response.ok) return null;
							const generation = (await response.json()) as Generation;
							return generation.state !== 'failed' && generation.assets.length > 0
								? generation
								: null;
						})
					)
				).filter((generation): generation is Generation => generation !== null);

	for (const generation of fetched) {
		resolved.set(generation.id, generation);
	}

	studio.starredExtras = studio.starredIds.flatMap((id) => {
		if (historyIds.has(id)) return [];
		const generation = resolved.get(id);
		return generation !== undefined ? [generation] : [];
	});
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
	studio.starredIds = isStarred(id)
		? studio.starredIds.filter((starredId) => starredId !== id)
		: [...studio.starredIds, id];
	saveStarredIds(studio.starredIds);
	// Best-effort refresh: a failed fetch leaves the previous starred set
	// in place and the next history load retries it.
	void loadStarredGenerations().catch(() => {});
}

export async function pollWhileWorking(): Promise<void> {
	if (polling) return;
	polling = true;
	try {
		while (studio.history.some((g) => g.state === 'queued' || g.state === 'running')) {
			await new Promise((resolve) => setTimeout(resolve, 1500));
			try {
				await loadHistory();
			} catch {
				continue;
			}
		}
	} finally {
		polling = false;
	}
}
