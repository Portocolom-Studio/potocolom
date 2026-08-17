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

function numberSpec(prop: ModelParamProperty | undefined, fallback: ParamRange): ParamRange {
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

export function modelProperty(
	model: Model | undefined,
	key: string
): ModelParamProperty | undefined {
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

export function strengthSpec(model: Model | undefined): ParamRange {
	return numberSpec(modelProperty(model, 'strength'), {
		min: 0,
		max: 1,
		default: 0.7,
		step: 0.1,
		integer: false
	});
}

export function structureStrengthSpec(model: Model | undefined): ParamRange {
	return numberSpec(modelProperty(model, 'structure_strength'), {
		min: 0,
		max: 1.5,
		default: 0.7,
		step: 0.1,
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

// One arrow key press moves one notch, so this is also the largest number of
// presses it can take to cross a range. A slider is the wrong control past it.
export const MAX_TRACK_NOTCHES = 2000;

export function trackSteps(spec: ParamRange): number {
	// How many presses of an arrow key cross the whole range. The slider runs
	// on this many notches rather than on a fixed 0-100 track, so one press
	// moves one parameter step: steps spans 2 to 8 on the realtime models, and
	// one percent of that track was 0.06 of a step, about seventeen presses to
	// advance by one (issue #250). Values below one step round to the same
	// number in normToValue anyway, so this changes how many presses a value
	// costs, not which values are reachable.
	if (spec.step <= 0 || spec.max <= spec.min) return 1;
	// The cap is a guard against a manifest, not a design choice: the slider
	// builds an array with one entry per notch, so a wide range against a tiny
	// step would hand it an unbounded one and freeze the page. Past the cap a
	// press moves more than one step, which is the thing this function exists
	// to prevent, so the bound is set where that stops being true for anything
	// plausible rather than at a round number: 2000 notches covers a 0 to 1
	// parameter at 0.0005, or 0 to 100 at 0.05. The widest a shipped manifest
	// asks for is 49 notches, from the steps fallback.
	return Math.min(MAX_TRACK_NOTCHES, Math.max(1, Math.round((spec.max - spec.min) / spec.step)));
}

export function formatParamValue(value: number, spec: ParamRange): string {
	if (spec.integer) return String(Math.round(value));
	if (spec.step < 1) return value.toFixed(1);
	return String(value);
}
