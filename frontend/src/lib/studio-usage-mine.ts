import { METRICS_RANGE_MS, type MetricsRange } from '$lib/studio-metrics-range';

export type UsageMeTotals = { events: number; gpu_ms: number; frames: number };
export type UsageCategoryRow = { category: string; events: number };
export type UsageModelRow = {
	model_id: string;
	events: number;
	avg_gpu_ms: number | null;
	p50_duration_ms: number | null;
};
export type UsageModelCategoryRow = {
	model_id: string;
	category: string;
	events: number;
	avg_duration_ms: number | null;
};
export type UsageMe = {
	from: string;
	to: string;
	totals: UsageMeTotals;
	by_category: UsageCategoryRow[];
	by_model: UsageModelRow[];
	by_model_category: UsageModelCategoryRow[];
};

export type FieldKind = 'string' | 'number' | 'nullable number';

function isRow(value: unknown, shape: Record<string, FieldKind>): boolean {
	if (typeof value !== 'object' || value === null) return false;
	const row = value as Record<string, unknown>;
	return Object.entries(shape).every(([field, kind]) => {
		const found = row[field];
		if (kind === 'string') return typeof found === 'string';
		if (kind === 'number') return typeof found === 'number';
		return found === null || typeof found === 'number';
	});
}

function isRows(value: unknown, shape: Record<string, FieldKind>): boolean {
	return Array.isArray(value) && value.every((row) => isRow(row, shape));
}

export function parseUsageMe(body: unknown): UsageMe | null {
	if (typeof body !== 'object' || body === null) return null;
	const candidate = body as Record<string, unknown>;
	if (typeof candidate.from !== 'string' || typeof candidate.to !== 'string') return null;
	if (!isRow(candidate.totals, { events: 'number', gpu_ms: 'number', frames: 'number' })) {
		return null;
	}
	if (!isRows(candidate.by_category, { category: 'string', events: 'number' })) return null;
	if (
		!isRows(candidate.by_model, {
			model_id: 'string',
			events: 'number',
			avg_gpu_ms: 'nullable number',
			p50_duration_ms: 'nullable number'
		})
	) {
		return null;
	}
	if (
		!isRows(candidate.by_model_category, {
			model_id: 'string',
			category: 'string',
			events: 'number',
			avg_duration_ms: 'nullable number'
		})
	) {
		return null;
	}
	return body as UsageMe;
}

export function usageMeWindow(range: MetricsRange, nowMs: number): { from: number; to: number } {
	return { from: nowMs - METRICS_RANGE_MS[range], to: nowMs };
}

export function usageMeSearch(fromMs: number, toMs: number): string {
	return `?${new URLSearchParams({ from: String(fromMs), to: String(toMs) })}`;
}

export async function fetchUsageMe(fromMs: number, toMs: number): Promise<UsageMe | null> {
	const response = await fetch(`/api/v1/usage/me${usageMeSearch(fromMs, toMs)}`);
	if (!response.ok) return null;
	return parseUsageMe(await response.json());
}

// Most events first, then the given names in order, so a tie never depends on
// the order the database happened to return.
export function byEventsThen<T extends { events: number }>(
	rows: readonly T[],
	key: (row: T) => readonly string[]
): T[] {
	return [...rows].sort((left, right) => {
		if (right.events !== left.events) return right.events - left.events;
		const a = key(left);
		const b = key(right);
		const shared = Math.min(a.length, b.length);
		for (let index = 0; index < shared; index += 1) {
			if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1;
		}
		return a.length - b.length;
	});
}

export function sharePct(events: number, total: number): number {
	if (total <= 0) return 0;
	return Math.round((events * 100) / total);
}

export type CategoryShare = UsageCategoryRow & { pct: number };

export function categoryShares(rows: readonly UsageCategoryRow[], total: number): CategoryShare[] {
	return byEventsThen(rows, (row) => [row.category]).map((row) => ({
		...row,
		pct: sharePct(row.events, total)
	}));
}
