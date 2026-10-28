import type { ImageFilters, ImageRangePreset, ImageSizeBin } from './image-filters';
import { defaultImageFilters } from './image-filters';
import type { MetricsTab } from './studio-view';
import type { MetricsRange } from './studio-metrics-range';
import type { LineageTreeOffset, LineageViewport } from './studio.svelte';
import { clampLineageCoordinate } from './lineage-canvas-state';
import { readStored, writeStored } from './safe-storage';
import { localDayStart } from './image-filters';

export type ViewOwner = { kind: 'local' } | { kind: 'account'; id: string };

export type MetricsPreferences = {
	tab: MetricsTab;
	usageRange: MetricsRange;
	mineRange: MetricsRange;
	selectedSessionId: string | null;
};

export type ImagesPreferences = {
	mode: 'grid' | 'lineage';
	filters: ImageFilters;
	action: 'all' | 'generate' | 'image_to_image' | 'upscale';
	search: string;
	starredOnly: boolean;
	gridAnchor: { id: string; beforeCursor: string | null; offset: number } | null;
	gridSelectionId: string | null;
	lineageSelection: { rootId: string; assetId: string } | null;
};

export type ViewPrefsSection = 'metrics' | 'images' | 'lineage-viewport' | 'lineage-offsets';
export type LineageOffsets = Record<string, LineageTreeOffset>;
export type SectionValueMap = {
	metrics: MetricsPreferences;
	images: ImagesPreferences;
	'lineage-viewport': LineageViewport | null;
	'lineage-offsets': LineageOffsets;
};
export type SectionValue = SectionValueMap[ViewPrefsSection];
export type PrefStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem' | 'length' | 'key'>;

export const DEFAULT_METRICS_PREFERENCES: MetricsPreferences = {
	tab: 'usage',
	usageRange: '5m',
	mineRange: '30d',
	selectedSessionId: null
};

export const DEFAULT_IMAGES_PREFERENCES: ImagesPreferences = {
	mode: 'lineage',
	filters: { ...defaultImageFilters, models: [], sizes: [] },
	action: 'all',
	search: '',
	starredOnly: false,
	gridAnchor: null,
	gridSelectionId: null,
	lineageSelection: null
};

const VERSION = 1;
const PREFIX = 'potocolom-view-v1';
const INDEX_KEY = `${PREFIX}:profiles`;
const PROFILE_LIMIT = 8;
const SECTION_LIMIT: Record<ViewPrefsSection, number> = {
	metrics: 4 * 1024,
	images: 48 * 1024,
	'lineage-viewport': 2 * 1024,
	'lineage-offsets': 128 * 1024
};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const METRICS_TABS = new Set<MetricsTab>(['usage', 'benchmarks', 'mine']);
const METRICS_RANGES = new Set<MetricsRange>(['5m', '1h', '24h', '7d', '30d']);
const IMAGE_RANGES = new Set<ImageRangePreset>(['all', 'today', 'last7', 'last30', 'custom']);
const IMAGE_SIZES = new Set<ImageSizeBin>(['512', '768', '1k', '2k', '4k', 'above_4k', 'unknown']);
const IMAGE_ACTIONS = new Set<ImagesPreferences['action']>([
	'all',
	'generate',
	'image_to_image',
	'upscale'
]);

function isRecord(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function boundedServerId(value: unknown): string | null {
	return typeof value === 'string' && UUID.test(value) ? value.toLowerCase() : null;
}

function boundedSessionId(value: unknown): string | null {
	return typeof value === 'string' && value.length > 0 && value.length <= 64 ? value : null;
}

function cleanFilters(value: unknown): ImageFilters {
	if (!isRecord(value)) return { ...DEFAULT_IMAGES_PREFERENCES.filters };
	const models = Array.isArray(value.models)
		? [
				...new Set(
					value.models.filter(
						(item): item is string =>
							typeof item === 'string' && item.trim() !== '' && item.length <= 128
					)
				)
			].slice(0, 128)
		: [];
	const sizes = Array.isArray(value.sizes)
		? [
				...new Set(
					value.sizes.filter(
						(item): item is ImageSizeBin =>
							typeof item === 'string' && IMAGE_SIZES.has(item as ImageSizeBin)
					)
				)
			].slice(0, 7)
		: [];
	const range =
		typeof value.range === 'string' && IMAGE_RANGES.has(value.range as ImageRangePreset)
			? (value.range as ImageRangePreset)
			: 'all';
	const from = typeof value.from === 'string' && DATE.test(value.from) ? value.from : '';
	const through =
		typeof value.through === 'string' && DATE.test(value.through) ? value.through : '';
	const datesValid =
		range !== 'custom' ||
		(from !== '' &&
			through !== '' &&
			localDayStart(from) !== null &&
			localDayStart(through) !== null &&
			from <= through);
	return {
		models,
		sizes,
		range: datesValid ? range : 'all',
		from: datesValid ? from : '',
		through: datesValid ? through : ''
	};
}

export function defaultViewPrefs<S extends ViewPrefsSection>(section: S): SectionValueMap[S] {
	if (section === 'metrics') return { ...DEFAULT_METRICS_PREFERENCES } as SectionValueMap[S];
	if (section === 'images')
		return structuredClone(DEFAULT_IMAGES_PREFERENCES) as SectionValueMap[S];
	if (section === 'lineage-viewport') return null as SectionValueMap[S];
	return {} as SectionValueMap[S];
}

export function parseViewPrefs<S extends ViewPrefsSection>(
	section: S,
	input: unknown
): SectionValueMap[S] {
	if (!isRecord(input) || input.version !== VERSION || input.section !== section) {
		return defaultViewPrefs(section);
	}
	const value = input.value;
	if (section === 'metrics') {
		if (!isRecord(value)) return defaultViewPrefs(section);
		return {
			tab:
				typeof value.tab === 'string' && METRICS_TABS.has(value.tab as MetricsTab)
					? (value.tab as MetricsTab)
					: 'usage',
			usageRange:
				typeof value.usageRange === 'string' && METRICS_RANGES.has(value.usageRange as MetricsRange)
					? (value.usageRange as MetricsRange)
					: '5m',
			mineRange:
				typeof value.mineRange === 'string' && METRICS_RANGES.has(value.mineRange as MetricsRange)
					? (value.mineRange as MetricsRange)
					: '30d',
			selectedSessionId: boundedSessionId(value.selectedSessionId)
		} as SectionValueMap[S];
	}
	if (section === 'images') {
		if (!isRecord(value)) return defaultViewPrefs(section);
		const anchor =
			isRecord(value.gridAnchor) &&
			boundedServerId(value.gridAnchor.id) !== null &&
			(value.gridAnchor.beforeCursor === null ||
				boundedServerId(value.gridAnchor.beforeCursor) !== null) &&
			typeof value.gridAnchor.offset === 'number' &&
			Number.isFinite(value.gridAnchor.offset)
				? {
						id: value.gridAnchor.id as string,
						beforeCursor: value.gridAnchor.beforeCursor as string | null,
						offset: Math.max(-100000, Math.min(100000, value.gridAnchor.offset))
					}
				: null;
		const lineageSelection =
			isRecord(value.lineageSelection) &&
			boundedServerId(value.lineageSelection.rootId) &&
			boundedServerId(value.lineageSelection.assetId)
				? {
						rootId: value.lineageSelection.rootId as string,
						assetId: value.lineageSelection.assetId as string
					}
				: null;
		return {
			mode:
				value.mode === 'grid' || value.mode === 'lineage'
					? value.mode
					: DEFAULT_IMAGES_PREFERENCES.mode,
			filters: cleanFilters(value.filters),
			action:
				typeof value.action === 'string' &&
				IMAGE_ACTIONS.has(value.action as ImagesPreferences['action'])
					? (value.action as ImagesPreferences['action'])
					: 'all',
			search: typeof value.search === 'string' ? value.search.slice(0, 1000) : '',
			starredOnly: value.starredOnly === true,
			gridAnchor: anchor,
			gridSelectionId: boundedServerId(value.gridSelectionId),
			lineageSelection
		} as SectionValueMap[S];
	}
	if (section === 'lineage-viewport') {
		if (
			!isRecord(value) ||
			![value.translateX, value.translateY, value.scale].every(
				(n) => typeof n === 'number' && Number.isFinite(n)
			) ||
			(value.scale as number) <= 0
		)
			return null as SectionValueMap[S];
		return {
			translateX: clampLineageCoordinate(value.translateX as number),
			translateY: clampLineageCoordinate(value.translateY as number),
			scale: value.scale as number,
			rootId: boundedServerId(value.rootId),
			anchorX:
				typeof value.anchorX === 'number' && Number.isFinite(value.anchorX)
					? clampLineageCoordinate(value.anchorX)
					: null,
			anchorY:
				typeof value.anchorY === 'number' && Number.isFinite(value.anchorY)
					? clampLineageCoordinate(value.anchorY)
					: null
		} as SectionValueMap[S];
	}
	if (!isRecord(value)) return {} as SectionValueMap[S];
	const entries = Object.entries(value).filter(
		([key, item]) =>
			UUID.test(key) &&
			isRecord(item) &&
			typeof item.x === 'number' &&
			Number.isFinite(item.x) &&
			typeof item.y === 'number' &&
			Number.isFinite(item.y)
	);
	return Object.fromEntries(
		entries.slice(-1024).map(([key, item]) => [
			key,
			{
				x: clampLineageCoordinate((item as { x: number }).x),
				y: clampLineageCoordinate((item as { y: number }).y)
			}
		])
	) as SectionValueMap[S];
}

export function normalizeViewPrefs<S extends ViewPrefsSection>(
	section: S,
	value: SectionValueMap[S]
): SectionValueMap[S] {
	return parseViewPrefs(section, { version: VERSION, section, value });
}

function ownerKey(owner: ViewOwner): string {
	return owner.kind === 'local'
		? 'local'
		: UUID.test(owner.id)
			? `account-${owner.id.toLowerCase()}`
			: 'unknown';
}

export function viewPrefsKey(owner: ViewOwner, section: ViewPrefsSection): string {
	return `${PREFIX}:${ownerKey(owner)}:${section}`;
}

function storageOrDefault(storage?: PrefStorage): PrefStorage | null {
	if (storage) return storage;
	try {
		return localStorage;
	} catch {
		return null;
	}
}

function utf8Length(value: string): number {
	return new TextEncoder().encode(value).length;
}

function parsedIndex(storage: PrefStorage): Array<{ id: string; at: number }> {
	try {
		const raw = storage.getItem(INDEX_KEY) ?? '[]';
		if (utf8Length(raw) > 4096) return [];
		const parsed: unknown = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		return parsed
			.slice(-128)
			.filter(
				(entry): entry is { id: string; at: number } =>
					isRecord(entry) &&
					typeof entry.id === 'string' &&
					/^account-[0-9a-f-]{36}$/i.test(entry.id) &&
					typeof entry.at === 'number' &&
					Number.isFinite(entry.at)
			);
	} catch {
		return [];
	}
}

export function activateViewPrefsOwner(
	owner: ViewOwner,
	now = Date.now(),
	supplied?: PrefStorage
): void {
	if (owner.kind === 'local') return;
	const storage = storageOrDefault(supplied);
	if (!storage || ownerKey(owner) === 'unknown') return;
	const id = ownerKey(owner);
	const indexed = parsedIndex(storage);
	const discovered = new Set(indexed.map((item) => item.id));
	try {
		const count = Math.min(storage.length, 4096);
		for (let i = 0; i < count; i++) {
			const key = storage.key(i);
			const match = key?.match(
				new RegExp(
					`^${PREFIX}:(account-[0-9a-f-]{36}):(metrics|images|lineage-viewport|lineage-offsets)$`,
					'i'
				)
			);
			if (match) discovered.add(match[1]);
		}
	} catch {}
	const timeById = new Map(indexed.map((item) => [item.id, item.at]));
	const profiles = [...discovered]
		.filter((profileId) => profileId !== id)
		.map((profileId) => ({ id: profileId, at: timeById.get(profileId) ?? 0 }));
	profiles.sort((a, b) => a.at - b.at);
	while (profiles.length >= PROFILE_LIMIT) {
		const evicted = profiles.shift();
		if (!evicted) break;
		for (const section of ['metrics', 'images', 'lineage-viewport', 'lineage-offsets'] as const) {
			try {
				storage.removeItem(`${PREFIX}:${evicted.id}:${section}`);
			} catch {}
		}
	}
	profiles.push({ id, at: now });
	try {
		storage.setItem(INDEX_KEY, JSON.stringify(profiles));
	} catch {}
}

export function readViewPrefs<S extends ViewPrefsSection>(
	owner: ViewOwner | null,
	section: S,
	supplied?: PrefStorage
): SectionValueMap[S] {
	if (owner === null || ownerKey(owner) === 'unknown') return defaultViewPrefs(section);
	const storage = storageOrDefault(supplied);
	if (!storage) return defaultViewPrefs(section);
	try {
		const raw = storage.getItem(viewPrefsKey(owner, section));
		if (!raw || utf8Length(raw) > SECTION_LIMIT[section]) return defaultViewPrefs(section);
		return parseViewPrefs(section, JSON.parse(raw));
	} catch {
		return defaultViewPrefs(section);
	}
}

export function hasViewPrefs(
	owner: ViewOwner,
	section: ViewPrefsSection,
	supplied?: PrefStorage
): boolean {
	if (ownerKey(owner) === 'unknown') return false;
	const storage = storageOrDefault(supplied);
	if (!storage) return false;
	try {
		return storage.getItem(viewPrefsKey(owner, section)) !== null;
	} catch {
		return false;
	}
}

export function writeViewPrefs<S extends ViewPrefsSection>(
	owner: ViewOwner | null,
	section: S,
	value: SectionValueMap[S],
	supplied?: PrefStorage
): void {
	if (owner === null || ownerKey(owner) === 'unknown') return;
	const storage = storageOrDefault(supplied);
	if (!storage) return;
	const normalized = normalizeViewPrefs(section, value);
	const serialized = JSON.stringify({ version: VERSION, section, value: normalized });
	if (utf8Length(serialized) > SECTION_LIMIT[section]) return;
	try {
		if (storage.getItem(viewPrefsKey(owner, section)) !== serialized)
			storage.setItem(viewPrefsKey(owner, section), serialized);
	} catch {}
}
