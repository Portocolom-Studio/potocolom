import type { Generation } from '$lib/studio.svelte';
import type { GpuHistoryPoint } from '$lib/studio-gpu-history';
import type { GpuSample } from '$lib/studio-gpu-sampler';
import { METRICS_RANGE_MS, type MetricsRange } from '$lib/studio-metrics-range';

/* Deterministic sample data so the metrics dashboard is populated in dev and
   previews when no worker has ever connected. Seeded PRNG: same series every
   render, only shifted to the current clock. */

function mulberry32(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) >>> 0;
		let t = a;
		t = Math.imul(t ^ (t >>> 15), t | 1);
		t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

const DEMO_MODELS: Array<{ id: string; gpuMs: number }> = [
	{ id: 'sdxl-turbo', gpuMs: 850 },
	{ id: 'sd15-lcm', gpuMs: 420 },
	{ id: 'flux-schnell', gpuMs: 2400 }
];

export function demoHistory(now: number = Date.now()): Generation[] {
	const rand = mulberry32(42);
	const jobs: Generation[] = [];
	const count = 48;
	for (let index = 0; index < count; index += 1) {
		const model = DEMO_MODELS[Math.floor(rand() * DEMO_MODELS.length)];
		// running/queued jobs stay recent; finished jobs form a dense burst in
		// the last few minutes so lanes are visible and session utilization is
		// a plausible ratio instead of rounding to zero over a day-long span
		const ageMs = index < 4 ? rand() * 90 * 1000 : 5000 + rand() ** 2 * 170 * 1000;
		const createdMs = now - ageMs;
		const queueMs = 40 + rand() * (rand() > 0.85 ? 2200 : 320);
		const gpuMs = model.gpuMs * (0.75 + rand() * 0.5);
		const postprocessMs = 30 + rand() * 90;
		const roll = rand();
		const state =
			index < 2 ? 'running' : index < 4 ? 'queued' : roll < 0.94 ? 'succeeded' : 'failed';
		const finished = state === 'succeeded' || state === 'failed';
		jobs.push({
			id: `demo-${index}`,
			model_id: model.id,
			params: { prompt: 'demo' },
			state,
			progress: state === 'running' ? rand() : null,
			gpu_ms: finished ? Math.round(gpuMs) : null,
			input_fetch_ms: finished ? Math.round(10 + rand() * 40) : null,
			load_ms: finished ? Math.round(rand() < 0.15 ? 900 + rand() * 2200 : 8 + rand() * 30) : null,
			postprocess_ms: finished ? Math.round(postprocessMs) : null,
			failure_reason: state === 'failed' ? 'demo failure' : null,
			created_at: new Date(createdMs).toISOString(),
			dispatched_at: state === 'queued' ? null : new Date(createdMs + queueMs).toISOString(),
			finished_at: finished
				? new Date(createdMs + queueMs + gpuMs + postprocessMs).toISOString()
				: null,
			assets: []
		});
	}
	return jobs.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
}

function utilAt(rand: () => number, phase: number): number {
	const wave = 42 + 30 * Math.sin(phase * Math.PI * 2);
	const burst = rand() > 0.82 ? 25 + rand() * 20 : 0;
	return Math.max(2, Math.min(99, wave + burst + (rand() - 0.5) * 12));
}

function vramAt(phase: number): number {
	return 38 + 26 * (0.5 + 0.5 * Math.sin(phase * Math.PI * 6));
}

export function demoGpuHistory(range: MetricsRange, now: number = Date.now()): GpuHistoryPoint[] {
	const rand = mulberry32(7);
	const windowMs = METRICS_RANGE_MS[range];
	const steps = 96;
	const stepMs = windowMs / steps;
	const points: GpuHistoryPoint[] = [];
	for (let index = 0; index <= steps; index += 1) {
		const phase = index / steps;
		const util = utilAt(rand, phase);
		const vram = vramAt(phase);
		const spread = range === '5m' ? 0 : 6 + rand() * 8;
		points.push({
			ts: now - windowMs + index * stepMs,
			util_pct: Math.round(util),
			util_min: Math.round(Math.max(0, util - spread)),
			util_max: Math.round(Math.min(100, util + spread)),
			vram_used_pct: Math.round(vram),
			vram_min: Math.round(Math.max(0, vram - spread / 2)),
			vram_max: Math.round(Math.min(100, vram + spread / 2)),
			temperature_c: Math.round(55 + util / 4),
			power_w: Math.round(90 + util * 1.6)
		});
	}
	return points;
}

export function demoGpuSamples(now: number = Date.now()): GpuSample[] {
	const rand = mulberry32(11);
	const samples: GpuSample[] = [];
	const stepMs = 2000;
	const count = 150;
	for (let index = 0; index < count; index += 1) {
		const phase = index / count;
		const util = utilAt(rand, phase);
		samples.push({
			ts: now - (count - index) * stepMs,
			computePct: Math.round(util),
			vramUsedPct: Math.round(vramAt(phase)),
			queueDepth: rand() > 0.8 ? 1 : 0,
			runningCount: util > 30 ? 1 : 0,
			activeModel: util > 30 ? DEMO_MODELS[0].id : null,
			temperatureC: Math.round(55 + util / 4),
			powerW: Math.round(90 + util * 1.6),
			hardwareAvailable: true
		});
	}
	return samples;
}
