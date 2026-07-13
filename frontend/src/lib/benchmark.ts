export type BenchmarkResult = {
	prompt_id: number;
	title: string;
	category: string;
	model_id: string;
	variant: string;
	params: Record<string, unknown>;
	model_load_ms?: number;
	state: 'succeeded' | 'failed';
	gpu_ms?: number;
	wall_s?: number;
	width?: number;
	height?: number;
	file?: string;
	error?: string;
};

/** Reference timings only — not offered in the studio UI. */
export const CAPPED_BENCHMARK_MODELS = new Set([
	'sd-turbo',
	'sdxl-turbo',
	'krea-2-turbo'
]);

export function isReferenceOnlyModel(modelId: string): boolean {
	return CAPPED_BENCHMARK_MODELS.has(modelId);
}

export type ModelStats = {
	model_id: string;
	succeeded: number;
	failed: number;
	load_ms: number | null;
	avg_gpu_ms: number | null;
	median_gpu_ms: number | null;
	avg_wall_s: number;
};

export type BenchmarkReport = {
	created_at: string;
	target_vram_gb?: number;
	prompt_count: number;
	models: string[];
	variants_per_prompt: number;
	total_jobs: number;
	succeeded: number;
	failed: number;
	model_stats: ModelStats[];
	results: BenchmarkResult[];
};

export function formatMs(ms: number | null | undefined): string {
	if (ms == null) return '—';
	if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`;
	return `${Math.round(ms)} ms`;
}

export function formatSeconds(seconds: number | null | undefined): string {
	if (seconds == null) return '—';
	return `${seconds.toFixed(1)} s`;
}

/** Average gpu_ms per prompt for one model (successful runs only). */
export function promptAverages(modelId: string, results: BenchmarkResult[]) {
	const buckets = new Map<number, { title: string; category: string; values: number[] }>();
	for (const row of results) {
		if (row.model_id !== modelId || row.state !== 'succeeded' || row.gpu_ms == null) continue;
		const bucket = buckets.get(row.prompt_id) ?? {
			title: row.title,
			category: row.category,
			values: []
		};
		bucket.values.push(row.gpu_ms);
		buckets.set(row.prompt_id, bucket);
	}
	return [...buckets.entries()]
		.map(([id, bucket]) => ({
			id,
			title: bucket.title,
			category: bucket.category,
			avg_gpu_ms: bucket.values.reduce((a, b) => a + b, 0) / bucket.values.length
		}))
		.sort((a, b) => a.id - b.id);
}

/** Average gpu_ms per variant label for one model. */
export function variantAverages(modelId: string, results: BenchmarkResult[]) {
	const buckets = new Map<string, number[]>();
	for (const row of results) {
		if (row.model_id !== modelId || row.state !== 'succeeded' || row.gpu_ms == null) continue;
		const values = buckets.get(row.variant) ?? [];
		values.push(row.gpu_ms);
		buckets.set(row.variant, values);
	}
	return [...buckets.entries()].map(([variant, values]) => ({
		variant,
		avg_gpu_ms: values.reduce((a, b) => a + b, 0) / values.length,
		count: values.length
	}));
}

export type ComparisonBar = {
	id: string;
	label: string;
	value: number;
	display: string;
	reference?: boolean;
};

const CHART_COLORS = [
	'var(--chart-1)',
	'var(--chart-2)',
	'var(--chart-3)',
	'var(--chart-4)',
	'var(--chart-5)'
] as const;

export function chartColor(index: number): string {
	return CHART_COLORS[index % CHART_COLORS.length];
}

/** Bar width 2–100 %; optional log scale for load times that span orders of magnitude. */
export function barScale(value: number, max: number, log = false): number {
	if (value <= 0 || max <= 0) return 0;
	if (log) {
		const logMax = Math.log10(Math.max(max, 1));
		const logVal = Math.log10(Math.max(value, 1));
		return Math.max(2, (logVal / logMax) * 100);
	}
	return Math.max(2, (value / max) * 100);
}

export function modelMetricBars(
	modelStats: ModelStats[],
	metric: 'avg_gpu_ms' | 'median_gpu_ms' | 'avg_wall_s' | 'load_ms',
	format: (value: number) => string,
	getValue: (row: ModelStats) => number | null = (row) => row[metric] as number | null
): ComparisonBar[] {
	const bars: ComparisonBar[] = [];
	for (const row of modelStats) {
		const raw = getValue(row);
		if (raw == null) continue;
		bars.push({
			id: row.model_id,
			label: row.model_id,
			value: raw,
			display: format(raw),
			reference: isReferenceOnlyModel(row.model_id)
		});
	}
	return bars.sort((a, b) => a.value - b.value);
}

export type DualMetricBar = {
	id: string;
	label: string;
	gpu_s: number;
	wall_s: number;
	reference?: boolean;
};

/** Per-model GPU denoise vs end-to-end wall time (seconds). */
export function gpuVsWallBars(modelStats: ModelStats[]): DualMetricBar[] {
	return modelStats
		.filter((row) => row.avg_gpu_ms != null)
		.map((row) => ({
			id: row.model_id,
			label: row.model_id,
			gpu_s: (row.avg_gpu_ms as number) / 1000,
			wall_s: row.avg_wall_s,
			reference: isReferenceOnlyModel(row.model_id)
		}))
		.sort((a, b) => a.gpu_s - b.gpu_s);
}

/** Average GPU ms per prompt category across all models (for heatmap-style comparison). */
export function categoryAverages(results: BenchmarkResult[]) {
	const buckets = new Map<string, Map<string, number[]>>();
	for (const row of results) {
		if (row.state !== 'succeeded' || row.gpu_ms == null) continue;
		const byModel = buckets.get(row.category) ?? new Map();
		const values = byModel.get(row.model_id) ?? [];
		values.push(row.gpu_ms);
		byModel.set(row.model_id, values);
		buckets.set(row.category, byModel);
	}
	return [...buckets.entries()].map(([category, byModel]) => ({
		category,
		models: [...byModel.entries()].map(([model_id, values]) => ({
			model_id,
			avg_gpu_ms: values.reduce((a, b) => a + b, 0) / values.length
		}))
	}));
}

export type LeaderboardRow = {
	rank: number;
	model_id: string;
	gpu_ms: number;
	wall_s: number;
	load_ms: number;
	gpu_display: string;
	wall_display: string;
	load_display: string;
	reference: boolean;
	gpu_ratio: number;
	wall_ratio: number;
	load_ratio: number;
};

export function leaderboardRows(modelStats: ModelStats[]): LeaderboardRow[] {
	const sorted = [...modelStats].sort(
		(a, b) => (a.avg_gpu_ms ?? Number.POSITIVE_INFINITY) - (b.avg_gpu_ms ?? Number.POSITIVE_INFINITY)
	);
	const maxGpu = Math.max(...sorted.map((r) => r.avg_gpu_ms ?? 0), 1);
	const maxWall = Math.max(...sorted.map((r) => r.avg_wall_s), 0.001);
	const maxLoad = Math.max(...sorted.map((r) => r.load_ms ?? 0), 1);

	return sorted.map((row, index) => {
		const gpu_ms = row.avg_gpu_ms ?? 0;
		const load_ms = row.load_ms ?? 0;
		return {
			rank: index + 1,
			model_id: row.model_id,
			gpu_ms,
			wall_s: row.avg_wall_s,
			load_ms,
			gpu_display: formatMs(gpu_ms),
			wall_display: formatSeconds(row.avg_wall_s),
			load_display: formatMs(load_ms),
			reference: isReferenceOnlyModel(row.model_id),
			gpu_ratio: gpu_ms / maxGpu,
			wall_ratio: row.avg_wall_s / maxWall,
			load_ratio: load_ms / maxLoad
		};
	});
}

export type CategoryLineSeries = {
	model_id: string;
	color: string;
	points: { category: string; avg_gpu_ms: number }[];
};

export function categoryLineSeries(
	results: BenchmarkResult[],
	modelIds: string[]
): { categories: string[]; series: CategoryLineSeries[] } {
	const rows = categoryAverages(results);
	const categories = rows.map((r) => r.category).sort();
	const series = modelIds.map((model_id, index) => ({
		model_id,
		color: chartColor(index),
		points: categories.map((category) => {
			const row = rows.find((r) => r.category === category);
			const model = row?.models.find((m) => m.model_id === model_id);
			return { category, avg_gpu_ms: model?.avg_gpu_ms ?? 0 };
		})
	}));
	return { categories, series };
}

export type MetricKey = 'gpu' | 'wall' | 'load';

export function metricValue(row: LeaderboardRow, metric: MetricKey): number {
	if (metric === 'gpu') return row.gpu_ms;
	if (metric === 'wall') return row.wall_s * 1000;
	return row.load_ms;
}

export function metricRatio(row: LeaderboardRow, metric: MetricKey): number {
	if (metric === 'gpu') return row.gpu_ratio;
	if (metric === 'wall') return row.wall_ratio;
	return row.load_ratio;
}

export function metricDisplay(row: LeaderboardRow, metric: MetricKey): string {
	if (metric === 'gpu') return row.gpu_display;
	if (metric === 'wall') return row.wall_display;
	return row.load_display;
}

