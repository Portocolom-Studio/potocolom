import type { BenchmarkReport } from '$lib/benchmark';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => {
	const response = await fetch('/benchmark/results.json');
	if (!response.ok) {
		return { report: null };
	}
	const report = (await response.json()) as BenchmarkReport;
	if (!report.created_at || report.results.length === 0) {
		return { report: null };
	}
	return { report };
};
