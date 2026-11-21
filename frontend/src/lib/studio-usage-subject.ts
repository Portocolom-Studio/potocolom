import { usageMeSearch, usageMeWindow, type UsageMeTotals } from '$lib/studio-usage-mine';
import type { MetricsRange } from '$lib/studio-metrics-range';

export type SubjectUsage = {
	from: string;
	to: string;
	totals: UsageMeTotals;
};

export function subjectUsageWindow(
	range: MetricsRange,
	nowMs: number
): { from: number; to: number } {
	return usageMeWindow(range, nowMs);
}

function isTotals(value: unknown): value is UsageMeTotals {
	if (typeof value !== 'object' || value === null) return false;
	const row = value as Record<string, unknown>;
	return (
		typeof row.events === 'number' &&
		typeof row.gpu_ms === 'number' &&
		typeof row.frames === 'number'
	);
}

export function parseSubjectUsage(body: unknown): SubjectUsage | null {
	if (typeof body !== 'object' || body === null) return null;
	const candidate = body as Record<string, unknown>;
	if (typeof candidate.from !== 'string' || typeof candidate.to !== 'string') return null;
	if (!isTotals(candidate.totals)) return null;
	if ('by_category' in candidate || 'by_model' in candidate) return null;
	return { from: candidate.from, to: candidate.to, totals: candidate.totals };
}

// 'unbound' when the API answers 404: no binding, or a subject that is not
// eligible. null for every other failure (a refusal, a server error, a
// dropped connection, a body that is not the totals document), which the tab
// shows as an error worth retrying, not as a missing binding.
export async function fetchSubjectUsage(
	fromMs: number,
	toMs: number,
	signal?: AbortSignal
): Promise<SubjectUsage | 'unbound' | null> {
	try {
		const response = await fetch(`/api/v1/usage/subject${usageMeSearch(fromMs, toMs)}`, { signal });
		if (response.status === 404) return 'unbound';
		if (!response.ok) return null;
		return parseSubjectUsage(await response.json());
	} catch {
		return null;
	}
}
