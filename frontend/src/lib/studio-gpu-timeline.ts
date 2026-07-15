import type { Generation } from '$lib/studio.svelte';
import type { GpuSample } from '$lib/studio-gpu-sampler';

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
	blocks?: TimelineBlock[];
};

export type GpuTimeline = {
	windowStartMs: number;
	windowEndMs: number;
	lanes: TimelineLane[];
	hardwareAvailable: boolean;
};

const WINDOW_MS = 5 * 60 * 1000;
const MODEL_COLORS = [
	'oklch(0.62 0.14 250)',
	'oklch(0.68 0.12 165)',
	'oklch(0.72 0.13 55)',
	'oklch(0.66 0.11 320)',
	'oklch(0.7 0.1 25)'
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

export function buildGpuTimeline(
	history: Generation[],
	liveSamples: GpuSample[],
	hardwareVramPct: number | null = null
): GpuTimeline {
	const now = Date.now();
	const windowStartMs = now - WINDOW_MS;
	const windowEndMs = now;
	const blocks = jobBlocks(history, windowStartMs, windowEndMs);
	const modelIds = topModelIds(blocks, 4);
	const hardwareAvailable = liveSamples.some((sample) => sample.hardwareAvailable);

	let vramPoints = forwardFillPoints(
		windowedPoints(liveSamples, windowStartMs, (sample) => sample.vramUsedPct)
	);
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
			color: 'oklch(0.62 0.16 250)',
			points: windowedPoints(liveSamples, windowStartMs, (sample) => sample.computePct)
		},
		{
			id: 'vram',
			label: 'VRAM %',
			kind: 'metric',
			color: 'oklch(0.68 0.14 145)',
			points: vramPoints
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
