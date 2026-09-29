// Validation for the generate form fields that the browser cannot be trusted
// to gate: submit is client-side, so Count and Seed are checked here and the
// form runs with novalidate to keep the native bubble out of the way.

// The manifest's parameter schema for a number property (the seed entry), as
// far as validation needs it. Absent bounds fall back to the unsigned 32-bit
// range the worker accepts.
export type SeedParamSchema = {
	minimum?: number;
	maximum?: number;
};

export const DEFAULT_SEED_MAX = 4294967295;

export function countError(
	value: string | number,
	min: number,
	max: number
): 'app.gen.count_invalid' | null {
	// The input binding can hand over either form: the field starts as a
	// string and Svelte's number-input binding coerces user edits to numbers.
	const parsed = typeof value === 'number' ? value : Number(value);
	if (Number.isNaN(parsed) || !Number.isInteger(parsed) || parsed < min || parsed > max) {
		return 'app.gen.count_invalid';
	}
	return null;
}

export function seedError(
	value: number | null,
	schema: SeedParamSchema
): 'app.gen.seed_invalid' | null {
	// Empty is the "random seed" state.
	if (value === null) return null;
	const min = schema.minimum ?? 0;
	const max = schema.maximum ?? DEFAULT_SEED_MAX;
	if (!Number.isInteger(value) || value < min || value > max) {
		return 'app.gen.seed_invalid';
	}
	return null;
}
