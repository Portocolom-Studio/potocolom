import type { Generation } from '$lib/studio.svelte';

export type GpuHardware = {
	device: string;
	available?: boolean;
	util_pct?: number;
	vram_used_pct?: number;
	vram_used_bytes?: number;
	vram_total_bytes?: number;
	temperature_c?: number;
	power_w?: number;
};

export type GpuSample = {
	ts: number;
	computePct: number;
	vramUsedPct: number | null;
	queueDepth: number;
	runningCount: number;
	activeModel: string | null;
	temperatureC: number | null;
	powerW: number | null;
	hardwareAvailable: boolean;
};

const MAX_SAMPLES = 150;
const SAMPLE_MS = 2000;
const SAMPLE_TIMEOUT_MS = 5000;

let samples: GpuSample[] = [];
let timer: ReturnType<typeof setInterval> | null = null;
let listeners = new Set<() => void>();
let lastHardware: GpuHardware | null = null;
let samplerEpoch = 0;
let activeTick: { epoch: number; controller: AbortController } | null = null;

function notify(): void {
	for (const listener of listeners) {
		listener();
	}
}

function fallbackComputePct(history: Generation[]): number {
	const queued = history.filter((g) => g.state === 'queued');
	const running = history.filter((g) => g.state === 'running');
	if (running.length > 0) {
		const progress = running.reduce((sum, job) => sum + (job.progress ?? 0.5), 0) / running.length;
		return Math.round(Math.min(100, progress * 100));
	}
	if (queued.length > 0) return 8;
	return 0;
}

function vramPctFromHardware(hardware: GpuHardware | null): number | null {
	if (!hardware) return null;
	if (typeof hardware.vram_used_pct === 'number') {
		return Math.max(0, Math.min(100, hardware.vram_used_pct));
	}
	const used = hardware.vram_used_bytes;
	const total = hardware.vram_total_bytes;
	if (typeof used === 'number' && typeof total === 'number' && total > 0) {
		return Math.round((used * 100) / total);
	}
	return null;
}

function sampleFromHistory(history: Generation[], hardware: GpuHardware | null): GpuSample {
	const queued = history.filter((g) => g.state === 'queued');
	const running = history.filter((g) => g.state === 'running');
	const utilPct =
		typeof hardware?.util_pct === 'number'
			? Math.max(0, Math.min(100, hardware.util_pct))
			: fallbackComputePct(history);
	return {
		ts: Date.now(),
		computePct: utilPct,
		vramUsedPct: vramPctFromHardware(hardware),
		queueDepth: queued.length,
		runningCount: running.length,
		activeModel: running[0]?.model_id ?? queued[0]?.model_id ?? null,
		temperatureC: typeof hardware?.temperature_c === 'number' ? hardware.temperature_c : null,
		powerW: typeof hardware?.power_w === 'number' ? hardware.power_w : null,
		hardwareAvailable: hardware?.available === true
	};
}

async function fetchHardware(signal: AbortSignal): Promise<GpuHardware | null> {
	try {
		const response = await fetch('/api/v1/studio/gpu', { signal });
		if (!response.ok) return null;
		const body = (await response.json()) as { gpu?: GpuHardware };
		return body.gpu ?? null;
	} catch {
		return null;
	}
}

export function gpuSamples(): GpuSample[] {
	return samples;
}

export function subscribeGpuSamples(listener: () => void): () => void {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

export function startGpuSampler(history: () => Generation[]): void {
	if (timer !== null) return;
	const tick = async () => {
		if (activeTick !== null || timer === null) return;
		const request = { epoch: samplerEpoch, controller: new AbortController() };
		activeTick = request;
		const timeout = setTimeout(() => request.controller.abort(), SAMPLE_TIMEOUT_MS);
		try {
			const fetched = await fetchHardware(request.controller.signal);
			if (
				request.controller.signal.aborted ||
				request.epoch !== samplerEpoch ||
				timer === null ||
				activeTick !== request
			)
				return;
			if (fetched) lastHardware = fetched;
			const hardware = fetched ?? lastHardware;
			const point = sampleFromHistory(history(), hardware);
			samples = [...samples, point].slice(-MAX_SAMPLES);
			notify();
		} finally {
			clearTimeout(timeout);
			if (activeTick === request) activeTick = null;
		}
	};
	samplerEpoch += 1;
	timer = setInterval(() => void tick(), SAMPLE_MS);
	void tick();
}

export function stopGpuSampler(): void {
	samplerEpoch += 1;
	if (timer !== null) {
		clearInterval(timer);
		timer = null;
	}
	const request = activeTick;
	activeTick = null;
	request?.controller.abort();
}

export function clearGpuSamples(): void {
	samplerEpoch += 1;
	const request = activeTick;
	activeTick = null;
	request?.controller.abort();
	samples = [];
	lastHardware = null;
	notify();
}

export function lastGpuHardware(): GpuHardware | null {
	return lastHardware;
}
