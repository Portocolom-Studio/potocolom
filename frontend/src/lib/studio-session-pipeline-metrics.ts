import type { Generation } from '$lib/studio.svelte';

export type PipelineMetrics = {
	queueWaitP50: number | null;
	queueWaitP95: number | null;
	postprocessSharePct: number | null;
	effectiveUtilPct: number | null;
	failureRatePct: number | null;
};

function percentile(sorted: number[], p: number): number | null {
	if (sorted.length === 0) return null;
	const index = Math.ceil((p / 100) * sorted.length) - 1;
	return sorted[Math.max(0, index)];
}

function jobWallMs(generation: Generation): number {
	if (generation.dispatched_at && generation.finished_at) {
		const wall =
			new Date(generation.finished_at).getTime() - new Date(generation.dispatched_at).getTime();
		if (wall > 0) return wall;
	}
	return (
		(generation.gpu_ms ?? 0) +
		(generation.load_ms ?? 0) +
		(generation.input_fetch_ms ?? 0) +
		(generation.postprocess_ms ?? 0)
	);
}

export function computePipelineMetrics(history: Generation[]): PipelineMetrics {
	const terminal = history.filter(
		(generation) => generation.state === 'succeeded' || generation.state === 'failed'
	);
	const failed = terminal.filter((generation) => generation.state === 'failed');
	const failureRatePct = terminal.length ? (failed.length / terminal.length) * 100 : null;

	const succeeded = history.filter((generation) => generation.state === 'succeeded');
	const queueWaits: number[] = [];
	let totalGpu = 0;
	let totalWall = 0;
	let totalPost = 0;

	for (const generation of succeeded) {
		if (generation.dispatched_at && generation.created_at) {
			const wait =
				new Date(generation.dispatched_at).getTime() - new Date(generation.created_at).getTime();
			if (wait >= 0) queueWaits.push(wait);
		}
		totalGpu += generation.gpu_ms ?? 0;
		const wall = jobWallMs(generation);
		if (wall > 0) totalWall += wall;
		totalPost += generation.postprocess_ms ?? 0;
	}

	const sortedWait = [...queueWaits].sort((a, b) => a - b);
	return {
		queueWaitP50: percentile(sortedWait, 50),
		queueWaitP95: percentile(sortedWait, 95),
		postprocessSharePct: totalWall > 0 ? (totalPost / totalWall) * 100 : null,
		effectiveUtilPct: totalWall > 0 ? (totalGpu / totalWall) * 100 : null,
		failureRatePct
	};
}
