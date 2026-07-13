import type { Model } from '$lib/studio.svelte';

export type ModelParamProperty = {
	type?: string;
	minimum?: number;
	maximum?: number;
	default?: number;
	enum?: number[];
};

export type ParamRange = {
	min: number;
	max: number;
	default: number;
	step: number;
	integer: boolean;
};

const COUNT_RANGE: ParamRange = {
	min: 1,
	max: 8,
	default: 1,
	step: 1,
	integer: true
};

function inferStep(min: number, max: number, integer: boolean): number {
	if (integer) return 1;
	const span = max - min;
	if (span <= 3) return 0.1;
	if (span <= 15) return 0.5;
	return 1;
}

function numberSpec(
	prop: ModelParamProperty | undefined,
	fallback: ParamRange
): ParamRange {
	if (!prop) return fallback;
	const min = prop.minimum ?? fallback.min;
	const max = prop.maximum ?? fallback.max;
	const integer = prop.type === 'integer';
	const defaultValue =
		typeof prop.default === 'number'
			? integer
				? Math.round(prop.default)
				: prop.default
			: fallback.default;
	return {
		min,
		max,
		default: defaultValue,
		step: inferStep(min, max, integer),
		integer
	};
}

export function modelProperty(model: Model | undefined, key: string): ModelParamProperty | undefined {
	return model?.parameters.properties?.[key] as ModelParamProperty | undefined;
}

export function stepsSpec(model: Model | undefined): ParamRange {
	return numberSpec(modelProperty(model, 'steps'), {
		min: 1,
		max: 50,
		default: 20,
		step: 1,
		integer: true
	});
}

export function guidanceSpec(model: Model | undefined): ParamRange {
	return numberSpec(modelProperty(model, 'guidance'), {
		min: 0,
		max: 15,
		default: 6,
		step: 0.5,
		integer: false
	});
}

export function countSpec(): ParamRange {
	return COUNT_RANGE;
}

export function sizeOptions(model: Model | undefined): number[] {
	const values = modelProperty(model, 'width')?.enum;
	return values?.length ? values : [512, 768, 1024];
}

export function defaultSizeIndex(model: Model | undefined, options: number[]): number {
	const preferred = modelProperty(model, 'width')?.default;
	if (typeof preferred === 'number') {
		const index = options.indexOf(preferred);
		if (index >= 0) return index;
	}
	return 0;
}

export function valueToNorm(value: number, spec: ParamRange): number {
	if (spec.max <= spec.min) return 0;
	return Math.min(1, Math.max(0, (value - spec.min) / (spec.max - spec.min)));
}

export function normToValue(norm: number, spec: ParamRange): number {
	const clamped = Math.min(1, Math.max(0, norm));
	const raw = spec.min + clamped * (spec.max - spec.min);
	const stepped = spec.step > 0 ? Math.round(raw / spec.step) * spec.step : raw;
	const bounded = Math.min(spec.max, Math.max(spec.min, stepped));
	return spec.integer ? Math.round(bounded) : bounded;
}

export function enumIndexToNorm(index: number, count: number): number {
	if (count <= 1) return 0;
	return Math.min(1, Math.max(0, index / (count - 1)));
}

export function normToEnumIndex(norm: number, count: number): number {
	if (count <= 1) return 0;
	return Math.round(Math.min(1, Math.max(0, norm)) * (count - 1));
}

export function formatParamValue(value: number, spec: ParamRange): string {
	if (spec.integer) return String(Math.round(value));
	if (spec.step < 1) return value.toFixed(1);
	return String(value);
}
