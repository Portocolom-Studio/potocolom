// Shared studio state: the sidebar (model list, gallery) and the generate
// panel look at the same registry and history.

import { STUDIO_MODELS } from '$lib/studio-models';

export type Model = {
	id: string;
	name: string;
	capabilities: string[];
	default: boolean;
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
	created_at: string;
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

function withoutFailed(generations: Generation[]): Generation[] {
	return generations.filter((generation) => generation.state !== 'failed');
}

export const studio = $state({
	models: [] as Model[],
	modelId: '',
	prompt: '',
	selectedId: null as string | null, // generation pinned in the viewer
	history: [] as Generation[],
	historyRecent: [] as Generation[], // newest page; restored by "back to recent"
	historyHasMore: false,
	historyExtended: false,
	starredIds: loadStarredIds() as string[],
	starredExtras: [] as Generation[] // starred jobs fetched outside the history pages
});

let polling = false;

function applyModels(models: Model[]): void {
	studio.models = models;
	if (!studio.modelId && models.length > 0) {
		studio.modelId = (models.find((m) => m.default) ?? models[0]).id;
	}
}

export async function loadModels(): Promise<void> {
	applyModels(STUDIO_MODELS);

	// Prefer live worker registry when the API is available (self-hosted stack).
	try {
		const response = await fetch('/api/v1/models');
		if (!response.ok) return;
		applyModels((await response.json()) as Model[]);
	} catch {
		// Hardcoded list above is enough for the redesign preview.
	}
}

export async function loadHistory(): Promise<void> {
	const response = await fetch(`/api/v1/generations?limit=${HISTORY_LIMIT}`);
	if (!response.ok) return;
	const recent = withoutFailed((await response.json()) as Generation[]);
	studio.historyRecent = recent;
	studio.historyHasMore = recent.length === HISTORY_LIMIT;
	if (!studio.historyExtended) {
		studio.history = recent;
		await loadStarredGenerations();
		return;
	}
	// Keep older pages at the tail while refreshing the newest slice in place.
	const recentIds = new Set(recent.map((generation) => generation.id));
	const olderTail = studio.history.filter(
		(generation) => !recentIds.has(generation.id) && generation.state !== 'failed'
	);
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
		return withoutFailed((await response.json()) as Generation[]);
	};

	let page = await fetchPage(`cursor=${oldest.id}`);
	if (page === null) return false;

	let unique = page.filter((generation) => !existing.has(generation.id));

	// Older API processes ignore cursor and return the first page again.
	if (unique.length === 0 && page.length > 0) {
		page = await fetchPage(`offset=${studio.history.length}`);
		if (page === null) return false;
		unique = page.filter((generation) => !existing.has(generation.id));
	}

	if (unique.length === 0) {
		studio.historyHasMore = false;
		return false;
	}

	studio.history = [...studio.history, ...unique];
	studio.historyHasMore = page.length === HISTORY_LIMIT;
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
	const inHistory = new Set(studio.history.map((generation) => generation.id));
	const missingIds = studio.starredIds.filter((id) => !inHistory.has(id));
	if (missingIds.length === 0) {
		studio.starredExtras = [];
		return;
	}

	const fetched = await Promise.all(
		missingIds.map(async (id) => {
			const response = await fetch(`/api/v1/generations/${id}`);
			if (!response.ok) return null;
			const generation = (await response.json()) as Generation;
			return generation.state !== 'failed' && generation.assets.length > 0 ? generation : null;
		})
	);
	studio.starredExtras = fetched.filter(
		(generation): generation is Generation => generation !== null
	);
}

export function resetHistoryToRecent(): void {
	studio.history = studio.historyRecent;
	studio.historyExtended = false;
	studio.historyHasMore = studio.historyRecent.length === HISTORY_LIMIT;
}

export function isStarred(id: string): boolean {
	return studio.starredIds.includes(id);
}

export function toggleStarred(id: string): void {
	studio.starredIds = isStarred(id)
		? studio.starredIds.filter((starredId) => starredId !== id)
		: [...studio.starredIds, id];
	saveStarredIds(studio.starredIds);
}

export async function pollWhileWorking(): Promise<void> {
	if (polling) return;
	polling = true;
	try {
		while (studio.history.some((g) => g.state === 'queued' || g.state === 'running')) {
			await new Promise((resolve) => setTimeout(resolve, 1500));
			await loadHistory();
		}
	} finally {
		polling = false;
	}
}
