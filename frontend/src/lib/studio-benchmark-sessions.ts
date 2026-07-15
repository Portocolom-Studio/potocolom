import type { BenchmarkReport } from '$lib/benchmark';

export type BenchmarkSession = {
	id: string;
	label: string;
	createdAt: string;
	report: BenchmarkReport;
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
