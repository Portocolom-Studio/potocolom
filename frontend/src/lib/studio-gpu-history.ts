import type { MetricsRange } from '$lib/studio-metrics-range';

export type GpuHistoryPoint = {
	ts: number;
	util_pct: number | null;
	util_min?: number | null;
	util_max?: number | null;
	vram_used_pct: number | null;
	vram_min?: number | null;
	vram_max?: number | null;
	temperature_c?: number | null;
	power_w?: number | null;
};

type GpuHistoryResponse = {
	from: string;
	to: string;
	rollup: 'raw' | '5m';
	samples: Array<{
		ts: string;
		util_pct: number | null;
		util_min?: number | null;
		util_max?: number | null;
		vram_used_pct: number | null;
		vram_min?: number | null;
		vram_max?: number | null;
		temperature_c?: number | null;
		power_w?: number | null;
	}>;
};

export async function fetchGpuHistory(
	fromMs: number,
	toMs: number,
	rollup: 'auto' | 'raw' | '5m' = 'auto'
): Promise<GpuHistoryPoint[]> {
	const params = new URLSearchParams({
		from: String(fromMs),
		to: String(toMs),
		rollup
	});
	const response = await fetch(`/api/v1/metrics/gpu/history?${params}`);
	if (!response.ok) {
		return [];
	}
	const body = (await response.json()) as GpuHistoryResponse;
	return body.samples
		.map((sample) => ({
			ts: Date.parse(sample.ts),
			util_pct: sample.util_pct,
			util_min: sample.util_min,
			util_max: sample.util_max,
			vram_used_pct: sample.vram_used_pct,
			vram_min: sample.vram_min,
			vram_max: sample.vram_max,
			temperature_c: sample.temperature_c ?? null,
			power_w: sample.power_w ?? null
		}))
		.filter((point) => Number.isFinite(point.ts));
}

export function historyRollupForRange(range: MetricsRange): 'auto' | 'raw' | '5m' {
	return range === '5m' ? 'raw' : 'auto';
}
