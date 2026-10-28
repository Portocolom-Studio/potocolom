import { PUBLIC_SITE_MODE } from '$env/static/public';
import { getLocale } from '$lib/i18n.svelte';
import type { BenchmarkReport } from '$lib/benchmark';

// The marketing build is a static artifact with no API behind it, so asking for
// sessions there is a request that can only fail. Presentation gate only: this
// file ships in both bundles either way.
const landing = PUBLIC_SITE_MODE === 'landing';

export type BenchmarkSession = {
	id: string;
	label: string;
	createdAt: string;
	report: BenchmarkReport | null;
};

export const BENCHMARK_REPORT_CACHE_LIMIT = 4;

/** Keep the active report and a few recent reports in the tab-local cache. */
export function retainBenchmarkReports(
	sessions: readonly BenchmarkSession[],
	selectedSessionId: string | null,
	limit = BENCHMARK_REPORT_CACHE_LIMIT
): BenchmarkSession[] {
	const boundedLimit = Math.max(0, Math.floor(limit));
	const reports = sessions.filter((session) => session.report !== null);
	const retained = new Set<string>();
	if (
		boundedLimit > 0 &&
		selectedSessionId &&
		reports.some((session) => session.id === selectedSessionId)
	) {
		retained.add(selectedSessionId);
	}
	const recent = [...reports].sort((left, right) => {
		const leftAt = Date.parse(left.createdAt);
		const rightAt = Date.parse(right.createdAt);
		if (Number.isFinite(leftAt) && Number.isFinite(rightAt)) return rightAt - leftAt;
		if (Number.isFinite(leftAt)) return -1;
		if (Number.isFinite(rightAt)) return 1;
		return 0;
	});
	for (const session of recent) {
		if (retained.size >= boundedLimit) break;
		retained.add(session.id);
	}
	return sessions.map((session) =>
		session.report !== null && !retained.has(session.id) ? { ...session, report: null } : session
	);
}

/** Merge a refreshed list without dropping reports already loaded in this tab. */
export function mergeBenchmarkSessions(
	loaded: readonly BenchmarkSession[],
	previous: readonly BenchmarkSession[],
	selectedSessionId: string | null
): { sessions: BenchmarkSession[]; selectedSessionId: string | null } {
	const previousReports = new Map(
		previous
			.filter((session) => session.report !== null)
			.map((session) => [session.id, session.report])
	);
	const sessions = loaded.map((session) => ({
		...session,
		report: session.report ?? previousReports.get(session.id) ?? null
	}));
	const nextSelectedId =
		selectedSessionId && sessions.some((session) => session.id === selectedSessionId)
			? selectedSessionId
			: (sessions[0]?.id ?? null);
	return {
		sessions: retainBenchmarkReports(sessions, nextSelectedId),
		selectedSessionId: nextSelectedId
	};
}

function sessionLabel(createdAt: string): string {
	const date = new Date(createdAt);
	if (Number.isNaN(date.getTime())) return createdAt;
	return date.toLocaleString(getLocale(), {
		year: 'numeric',
		month: 'short',
		day: 'numeric',
		hour: '2-digit',
		minute: '2-digit'
	});
}

export async function loadBenchmarkSessions(
	allowApi = true,
	signal?: AbortSignal,
	options: { fallbackToStaticOnApiFailure?: boolean } = {}
): Promise<BenchmarkSession[]> {
	if (signal?.aborted) return [];
	if (!landing && allowApi) {
		try {
			const response = await fetch('/api/v1/benchmark/sessions', { signal });
			if (response.ok) {
				const summaries = (await response.json()) as { id: string; created_at: string }[];
				// An install that has never ingested a run still ships the committed
				// report, so an empty list falls through rather than showing nothing.
				if (summaries.length > 0) {
					return summaries.map((summary) => ({
						id: summary.id,
						label: sessionLabel(summary.created_at),
						createdAt: summary.created_at,
						report: null
					}));
				}
			}
			if (!response.ok && options.fallbackToStaticOnApiFailure === false) return [];
		} catch {
			if (signal?.aborted) return [];
			if (options.fallbackToStaticOnApiFailure === false) return [];
			// An install without a reachable API still has the committed report.
		}
	}
	if (signal?.aborted) return [];
	const response = await fetch('/benchmark/results.json', { signal });
	if (!response.ok) return [];
	let report: BenchmarkReport;
	try {
		report = (await response.json()) as BenchmarkReport;
	} catch {
		return [];
	}
	if (
		typeof report?.created_at !== 'string' ||
		!Array.isArray(report?.results) ||
		report.results.length === 0
	) {
		return [];
	}
	return [
		{
			id: report.created_at,
			label: sessionLabel(report.created_at),
			createdAt: report.created_at,
			report
		}
	];
}

export async function loadBenchmarkSessionReport(
	id: string,
	signal?: AbortSignal
): Promise<BenchmarkReport | null> {
	// Same guard as the listing above: this module ships in the marketing bundle
	// too, where there is no API to answer.
	if (landing) return null;
	try {
		const response = await fetch(`/api/v1/benchmark/sessions/${id}`, { signal });
		if (!response.ok) return null;
		return (await response.json()) as BenchmarkReport;
	} catch {
		return null;
	}
}
