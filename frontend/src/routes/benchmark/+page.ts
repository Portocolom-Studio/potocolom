import type { BenchmarkReport } from '$lib/benchmark';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => {
	const response = await fetch('/benchmark/results.json');
	if (!response.ok) {
		return { report: null };
	}
	let report: BenchmarkReport;
	try {
		report = await response.json();
	} catch {
		return { report: null };
	}
	if (
		typeof report?.created_at !== 'string' ||
		!Array.isArray(report?.results) ||
		report.results.length === 0
	) {
		return { report: null };
	}
	return { report };
};
