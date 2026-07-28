import { PUBLIC_SITE_MODE } from '$env/static/public';
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

function sessionLabel(createdAt: string): string {
	const date = new Date(createdAt);
	if (Number.isNaN(date.getTime())) return createdAt;
	return date.toLocaleString(undefined, {
		year: 'numeric',
		month: 'short',
		day: 'numeric',
		hour: '2-digit',
		minute: '2-digit'
	});
}

export async function loadBenchmarkSessions(): Promise<BenchmarkSession[]> {
	if (!landing) {
		try {
			const response = await fetch('/api/v1/benchmark/sessions');
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
		} catch {
			// An install without a reachable API still has the committed report.
		}
	}
	const response = await fetch('/benchmark/results.json');
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

export async function loadBenchmarkSessionReport(id: string): Promise<BenchmarkReport | null> {
	const response = await fetch(`/api/v1/benchmark/sessions/${id}`);
	if (!response.ok) return null;
	return (await response.json()) as BenchmarkReport;
}
