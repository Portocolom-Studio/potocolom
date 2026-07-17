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

export function estimateUpscaleGpuMs(model: Model | undefined, factor: number): number | null {
	const baseMs = model?.estimated_gpu_ms_default;
	if (baseMs == null || baseMs <= 0 || !Number.isFinite(factor) || factor <= 0) return null;
	const defaultFactor = Number(modelProperty(model, 'factor')?.default ?? 2);
	if (!Number.isFinite(defaultFactor) || defaultFactor <= 0) return Math.max(1, Math.round(baseMs));
	return Math.max(1, Math.round(baseMs * (factor / defaultFactor) ** 2));
}
