import { modelProperty, stepsSpec } from '$lib/model-params';
import type { Model } from '$lib/studio.svelte';

export function estimateGpuMs(
	model: Model | undefined,
	params: { steps: number; width: number; height: number }
): number | null {
	const baseMs = model?.estimated_gpu_ms_default;
	if (baseMs == null || baseMs <= 0) return null;

	const stepsDefault = stepsSpec(model).default;
	const widthDefault = Number(modelProperty(model, 'width')?.default ?? params.width);
	const heightDefault = Number(modelProperty(model, 'height')?.default ?? params.height);
	const inputs = [
		stepsDefault,
		widthDefault,
		heightDefault,
		params.steps,
		params.width,
		params.height
	];
	if (inputs.some((value) => !Number.isFinite(value) || value <= 0)) return null;

	const stepScale = params.steps / stepsDefault;
	const pixelScale = (params.width * params.height) / (widthDefault * heightDefault);
	const estimate = Math.round(baseMs * stepScale * pixelScale);
	return Number.isFinite(estimate) ? Math.max(1, estimate) : null;
}

// model_timings.json upscale baselines are measured on a 1024x1024 source.
const UPSCALE_TIMING_SOURCE_PIXELS = 1024 * 1024;

export function estimateUpscaleGpuMs(
	model: Model | undefined,
	factor: number,
	source?: { width: number; height: number }
): number | null {
	if (!Number.isFinite(factor) || factor <= 0) return null;
	// Measured per-factor numbers from the registry win; factor^2 scaling is
	// the fallback and is wrong for native-scale models (x4 net serving x2).
	const measured = model?.estimated_gpu_ms_by_factor?.[String(factor)];
	let ms: number | null = null;
	if (measured != null && measured > 0) {
		ms = measured;
	} else {
		const baseMs = model?.estimated_gpu_ms_default;
		if (baseMs == null || baseMs <= 0) return null;
		const defaultFactor = Number(modelProperty(model, 'factor')?.default ?? 2);
		if (!Number.isFinite(defaultFactor) || defaultFactor <= 0) {
			ms = baseMs;
		} else {
			ms = baseMs * (factor / defaultFactor) ** 2;
		}
	}
	if (source != null) {
		const pixels = source.width * source.height;
		if (Number.isFinite(pixels) && pixels > 0) {
			ms = ms * (pixels / UPSCALE_TIMING_SOURCE_PIXELS);
		}
	}
	return Math.max(1, Math.round(ms));
}
