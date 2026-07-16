import type { Generation } from '$lib/studio.svelte';
import type { GpuSample } from '$lib/studio-gpu-sampler';
import type { GpuHistoryPoint } from '$lib/studio-gpu-history';
import { GPU_METRIC_COLORS } from '$lib/studio-gpu-chart';
import { METRICS_RANGE_MS, type MetricsRange } from '$lib/studio-metrics-range';

export type TimelineBlock = {
	id: string;
	modelId: string;
	startMs: number;
	endMs: number;
	label: string;
};

export type TimelineLane = {
	id: string;
	label: string;
	kind: 'metric' | 'model';
	color: string;
	points?: { ts: number; value: number }[];
	bands?: { ts: number; min: number; max: number; mean: number }[];
	blocks?: TimelineBlock[];
};

export type GpuTimeline = {
	windowStartMs: number;
	windowEndMs: number;
	lanes: TimelineLane[];
	hardwareAvailable: boolean;
};

const MODEL_COLORS = [
	GPU_METRIC_COLORS.temperature,
	GPU_METRIC_COLORS.power,
	'var(--chart-5)',
	'var(--chart-4)'
];

function parseTime(iso: string): number {
	const ms = Date.parse(iso);
	return Number.isFinite(ms) ? ms : Date.now();
}

function jobBlocks(history: Generation[], windowStart: number, windowEnd: number): TimelineBlock[] {
	const blocks: TimelineBlock[] = [];
	for (const job of history) {
		if (job.state !== 'succeeded' && job.state !== 'running') continue;
		const startMs = parseTime(job.created_at);
		let endMs = windowEnd;
		if (job.state === 'succeeded' && job.gpu_ms != null) {
			endMs = startMs + job.gpu_ms;
		}
		if (endMs < windowStart || startMs > windowEnd) continue;
		blocks.push({
			id: job.id,
			modelId: job.model_id,
			startMs: Math.max(startMs, windowStart),
			endMs: Math.min(endMs, windowEnd),
			label: job.model_id
		});
	}
	return blocks;
}

function topModelIds(blocks: TimelineBlock[], limit: number): string[] {
	const counts = new Map<string, number>();
	for (const block of blocks) {
		counts.set(block.modelId, (counts.get(block.modelId) ?? 0) + 1);
	}
	return [...counts.entries()]
		.sort((a, b) => b[1] - a[1])
		.slice(0, limit)
		.map(([modelId]) => modelId);
}

function windowedPoints(
	samples: GpuSample[],
	windowStartMs: number,
	pick: (sample: GpuSample) => number | null
) {
	return samples
		.filter((sample) => sample.ts >= windowStartMs)
		.map((sample) => {
			const value = pick(sample);
			return value == null ? null : { ts: sample.ts, value };
		})
		.filter((point): point is { ts: number; value: number } => point !== null);
}

function forwardFillPoints(
	points: { ts: number; value: number }[]
): { ts: number; value: number }[] {
	let last: number | null = null;
	const filled: { ts: number; value: number }[] = [];
	for (const point of points) {
		if (point.value != null && !Number.isNaN(point.value)) {
			last = point.value;
		}
		if (last != null) {
			filled.push({ ts: point.ts, value: last });
		}
	}
	return filled;
}

function historyMetricPoints(
	history: GpuHistoryPoint[],
	pickValue: (point: GpuHistoryPoint) => number | null,
	pickMin: (point: GpuHistoryPoint) => number | null | undefined,
	pickMax: (point: GpuHistoryPoint) => number | null | undefined,
	windowStartMs: number
) {
	const points: { ts: number; value: number }[] = [];
	const bands: { ts: number; min: number; max: number; mean: number }[] = [];
	for (const sample of history) {
		if (sample.ts < windowStartMs) continue;
		const value = pickValue(sample);
		if (value == null) continue;
		points.push({ ts: sample.ts, value });
		const min = pickMin(sample);
		const max = pickMax(sample);
		if (min != null && max != null) {
			bands.push({ ts: sample.ts, min, max, mean: value });
		}
	}
	return { points, bands };
}

export function buildGpuTimeline(
	history: Generation[],
	liveSamples: GpuSample[],
	hardwareVramPct: number | null = null,
	range: MetricsRange = '5m',
	persistedHistory: GpuHistoryPoint[] = []
): GpuTimeline {
	const now = Date.now();
	const windowMs = METRICS_RANGE_MS[range];
	const windowStartMs = now - windowMs;
	const windowEndMs = now;
	const blocks = jobBlocks(history, windowStartMs, windowEndMs);
	const modelIds = topModelIds(blocks, 4);
	const hardwareAvailable =
		liveSamples.some((sample) => sample.hardwareAvailable) ||
		persistedHistory.some((sample) => sample.util_pct != null || sample.vram_used_pct != null);

	let computePoints = windowedPoints(liveSamples, windowStartMs, (sample) => sample.computePct);
	let computeBands: { ts: number; min: number; max: number; mean: number }[] = [];
	let vramPoints = forwardFillPoints(
		windowedPoints(liveSamples, windowStartMs, (sample) => sample.vramUsedPct)
	);
	let vramBands: { ts: number; min: number; max: number; mean: number }[] = [];

	if (range !== '5m' && persistedHistory.length > 0) {
		const compute = historyMetricPoints(
			persistedHistory,
			(point) => point.util_pct,
			(point) => point.util_min,
			(point) => point.util_max,
			windowStartMs
		);
		computePoints = compute.points;
		computeBands = compute.bands;

		const vram = historyMetricPoints(
			persistedHistory,
			(point) => point.vram_used_pct,
			(point) => point.vram_min,
			(point) => point.vram_max,
			windowStartMs
		);
		vramPoints = forwardFillPoints(vram.points);
		vramBands = vram.bands;
	}

	if (vramPoints.length === 0 && hardwareVramPct != null) {
		vramPoints = [
			{ ts: windowStartMs, value: hardwareVramPct },
			{ ts: windowEndMs, value: hardwareVramPct }
		];
	}

	const lanes: TimelineLane[] = [
		{
			id: 'compute',
			label: 'GPU %',
			kind: 'metric',
			color: GPU_METRIC_COLORS.compute,
			points: computePoints,
			bands: computeBands.length > 0 ? computeBands : undefined
		},
		{
			id: 'vram',
			label: 'VRAM %',
			kind: 'metric',
			color: GPU_METRIC_COLORS.vram,
			points: vramPoints,
			bands: vramBands.length > 0 ? vramBands : undefined
		}
	];

	for (const [index, modelId] of modelIds.entries()) {
		lanes.push({
			id: `model-${modelId}`,
			label: modelId,
			kind: 'model',
			color: MODEL_COLORS[index % MODEL_COLORS.length],
			blocks: blocks.filter((block) => block.modelId === modelId)
		});
	}

	return { windowStartMs, windowEndMs, lanes, hardwareAvailable };
}
