import type { Generation } from '$lib/studio.svelte';

export type ModelRuntimeRow = {
	modelId: string;
	count: number;
	totalGpuMs: number;
	avgGpuMs: number;
};

export type RecentRuntimeRow = {
	id: string;
	modelId: string;
	gpuMs: number;
	width: number;
	height: number;
	steps: number | null;
	prompt: string;
};

export type SessionMetrics = {
	count: number;
	totalGpuMs: number;
	avgGpuMs: number | null;
	medianGpuMs: number | null;
	byModel: ModelRuntimeRow[];
	recent: RecentRuntimeRow[];
};

function numParam(params: Record<string, unknown>, key: string): number | null {
	const value = params[key];
	return typeof value === 'number' ? value : null;
}

export function computeSessionMetrics(history: Generation[]): SessionMetrics {
	const finished = history.filter(
		(generation) => generation.state === 'succeeded' && generation.gpu_ms != null
	);
	const gpuValues = finished.map((generation) => generation.gpu_ms as number);

	const byModelMap = new Map<string, number[]>();
	for (const generation of finished) {
		const values = byModelMap.get(generation.model_id) ?? [];
		values.push(generation.gpu_ms as number);
		byModelMap.set(generation.model_id, values);
	}

	const byModel = [...byModelMap.entries()]
		.map(([modelId, values]) => ({
			modelId,
			count: values.length,
			totalGpuMs: values.reduce((sum, value) => sum + value, 0),
			avgGpuMs: values.reduce((sum, value) => sum + value, 0) / values.length
		}))
		.sort((a, b) => b.avgGpuMs - a.avgGpuMs);

	const recent = finished.slice(0, 12).map((generation) => ({
		id: generation.id,
		modelId: generation.model_id,
		gpuMs: generation.gpu_ms as number,
		width: numParam(generation.params, 'width') ?? generation.assets[0]?.width ?? 0,
		height: numParam(generation.params, 'height') ?? generation.assets[0]?.height ?? 0,
		steps: numParam(generation.params, 'steps'),
		prompt: (generation.params.prompt ?? '').trim()
	}));

	const sorted = [...gpuValues].sort((a, b) => a - b);
	const medianGpuMs =
		sorted.length === 0
			? null
			: sorted.length % 2 === 1
				? sorted[(sorted.length - 1) / 2]
				: (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;

	return {
		count: finished.length,
		totalGpuMs: gpuValues.reduce((sum, value) => sum + value, 0),
		avgGpuMs: gpuValues.length
			? gpuValues.reduce((sum, value) => sum + value, 0) / gpuValues.length
			: null,
		medianGpuMs,
		byModel,
		recent
	};
}
