// Model and date filters for the Images library. The two halves of the
// feature live here so the canvas component only wires state to them:
// URL building for the server-paged grid, and local matching for the rows
// the history strip has not loaded.
//
// Date bounds are whole days in the browser's own timezone. A date the user
// typed means "that day where I live", so the range runs from the local
// start of the From day (inclusive) to the local start of the day after
// Through (exclusive). Building them from year/month/day numbers keeps DST
// correct by construction; parsing 'YYYY-MM-DD' as an instant would slide
// the window by the UTC offset instead.

import type { Generation } from './studio.svelte';

export type ImageRangePreset = 'all' | 'today' | 'last7' | 'last30' | 'custom';
export type ImageSizeBin = '512' | '768' | '1k' | '2k' | '4k' | 'above_4k' | 'unknown';

export interface ImageModelOption {
	id: string;
	name: string;
}

export interface ImageFilters {
	/** Selected model ids. Empty means every model. */
	models: string[];
	/** Selected longest-side size bins. Empty means every size. */
	sizes: ImageSizeBin[];
	range: ImageRangePreset;
	/** Local days, 'YYYY-MM-DD'. Only meaningful when range is 'custom'. */
	from: string;
	through: string;
}

export const defaultImageFilters: ImageFilters = {
	models: [],
	sizes: [],
	range: 'all',
	from: '',
	through: ''
};

export function clearImageFilters(): ImageFilters {
	return { models: [], sizes: [], range: 'all', from: '', through: '' };
}

/** The library is narrowed when any filter differs from the default. */
export function imageFiltersActive(filters: ImageFilters): boolean {
	return filters.models.length > 0 || filters.sizes.length > 0 || filters.range !== 'all';
}

/** Selections are sets: two filters saying the same thing must not refetch. */
export function imageFiltersEqual(a: ImageFilters, b: ImageFilters): boolean {
	if (a.range !== b.range || a.from !== b.from || a.through !== b.through) return false;
	return sameSet(a.models, b.models) && sameSet(a.sizes, b.sizes);
}

function sameSet<T>(left: T[], right: T[]): boolean {
	const leftSet = new Set(left);
	const rightSet = new Set(right);
	return leftSet.size === rightSet.size && [...leftSet].every((value) => rightSet.has(value));
}

export const imageSizeBins: { value: ImageSizeBin; min: number | null; max: number | null }[] = [
	{ value: '512', min: 1, max: 640 },
	{ value: '768', min: 640, max: 896 },
	{ value: '1k', min: 896, max: 1536 },
	{ value: '2k', min: 1536, max: 3072 },
	{ value: '4k', min: 3072, max: 6144 },
	{ value: 'above_4k', min: 6144, max: null },
	{ value: 'unknown', min: null, max: null }
];

export function imageSizeBinForDimensions(width: number, height: number): ImageSizeBin | null {
	if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return null;
	const longestSide = Math.max(width, height);
	const matchingBin = imageSizeBins.find(
		(bin) =>
			bin.value !== 'unknown' &&
			bin.min !== null &&
			longestSide >= bin.min &&
			(bin.max === null || longestSide < bin.max)
	);
	return matchingBin?.value ?? null;
}

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * The local start of a 'YYYY-MM-DD' day, or null when the string is not a
 * real calendar day. Round-tripping the numbers rejects dates such as
 * 2026-02-30 that the Date constructor would silently roll into March.
 */
export function localDayStart(day: string): Date | null {
	const match = DAY.exec(day);
	if (!match) return null;
	const year = Number(match[1]);
	const month = Number(match[2]);
	const date = Number(match[3]);
	const start = new Date(year, month - 1, date);
	if (start.getFullYear() !== year || start.getMonth() !== month - 1 || start.getDate() !== date) {
		return null;
	}
	return start;
}

function plusDays(day: string, days: number): Date | null {
	const start = localDayStart(day);
	if (!start) return null;
	// Arithmetic on the day number, not on milliseconds: the local clock
	// skips an hour on the DST boundary and a day is still a day.
	return new Date(start.getFullYear(), start.getMonth(), start.getDate() + days);
}

/** A custom range reversed in the calendar makes no window at all. */
export function customRangeReversed(filters: ImageFilters): boolean {
	if (filters.range !== 'custom') return false;
	const from = localDayStart(filters.from);
	const through = localDayStart(filters.through);
	if (!from || !through) return false;
	return from.getTime() > through.getTime();
}

export interface DayWindow {
	from: Date;
	/** Exclusive: the local start of the day after the last selected day. */
	throughExclusive: Date;
}

/** The custom window, or null while a bound is missing or not a real day. */
export function customDayBounds(filters: ImageFilters): DayWindow | null {
	if (filters.range !== 'custom') return null;
	const from = localDayStart(filters.from);
	const throughExclusive = plusDays(filters.through, 1);
	if (!from || !throughExclusive) return null;
	if (from.getTime() >= throughExclusive.getTime()) return null;
	return { from, throughExclusive };
}

/**
 * A preset window over whole local days, including today. 'Last 7' is today
 * plus the six days before it, so the window is seven whole days whatever
 * the hour is now.
 */
export function presetDayRange(range: ImageRangePreset, now: Date): DayWindow | null {
	if (range !== 'today' && range !== 'last7' && range !== 'last30') return null;
	const back = range === 'today' ? 0 : range === 'last7' ? 6 : 29;
	const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
	const from = new Date(today.getFullYear(), today.getMonth(), today.getDate() - back);
	return {
		from,
		throughExclusive: new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1)
	};
}

function windowFor(filters: ImageFilters, now: Date): DayWindow | null {
	if (filters.range === 'all') return null;
	return filters.range === 'custom' ? customDayBounds(filters) : presetDayRange(filters.range, now);
}

/**
 * The active filters as query keys, or null when a custom range is reversed
 * or incomplete: the caller must not request anything until the range makes
 * sense, and the UI shows the reason instead.
 */
export function withImageFilters(
	query: URLSearchParams,
	filters: ImageFilters,
	now: Date = new Date()
): URLSearchParams | null {
	const next = new URLSearchParams(query);
	for (const model of filters.models) next.append('model_id', model);
	for (const size of new Set(filters.sizes)) next.append('size', size);
	if (filters.range === 'all') return next;
	if (filters.range === 'custom' && customRangeReversed(filters)) return null;
	const window = windowFor(filters, now);
	if (!window) return null;
	next.set('created_from', window.from.toISOString());
	next.set('created_before', window.throughExclusive.toISOString());
	return next;
}

type GenerationLike = Pick<Generation, 'model_id' | 'created_at' | 'assets'>;

/**
 * Whether a generation row belongs in the filtered library. Used where the
 * server cannot answer: a job that just finished, and a grid row opened from
 * an id the filtered history page did not contain. Its assets must be the
 * serialized master assets, without thumbnails or foreign-owned outputs.
 */
export function generationMatchesImageFilters(
	generation: GenerationLike,
	filters: ImageFilters,
	now: Date = new Date()
): boolean {
	if (!imageFiltersActive(filters)) return true;
	if (filters.models.length > 0 && !filters.models.includes(generation.model_id)) return false;
	if (filters.sizes.length > 0) {
		const knownBins = generation.assets
			.map((asset) => imageSizeBinForDimensions(asset.width, asset.height))
			.filter((bin): bin is Exclude<ImageSizeBin, 'unknown'> => bin !== null);
		const matchesSize = knownBins.some((bin) => filters.sizes.includes(bin));
		const isUnknown = knownBins.length === 0;
		if (!matchesSize && !(isUnknown && filters.sizes.includes('unknown'))) return false;
	}
	if (filters.range === 'all') return true;
	const window = windowFor(filters, now);
	if (!window) return false;
	const created = Date.parse(generation.created_at);
	if (!Number.isFinite(created)) return false;
	return created >= window.from.getTime() && created < window.throughExclusive.getTime();
}

/** The option name, falling back to the id so a label is never empty. */
export function modelOptionLabel(option: ImageModelOption | undefined): string {
	if (!option) return '';
	return option.name || option.id;
}
