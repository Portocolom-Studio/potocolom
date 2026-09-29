import assert from 'node:assert/strict';
import { test } from 'node:test';

import { countError, seedError } from './generate-validation.ts';

const INVALID_COUNT = 'app.gen.count_invalid';
const INVALID_SEED = 'app.gen.seed_invalid';

test('countError accepts a whole number at either bound', () => {
	assert.equal(countError('1', 1, 8), null);
	assert.equal(countError('8', 1, 8), null);
	assert.equal(countError('4', 1, 8), null);
});

test('countError refuses values the panel clamps anyway', () => {
	assert.equal(countError('0', 1, 8), INVALID_COUNT);
	assert.equal(countError('-3', 1, 8), INVALID_COUNT);
	assert.equal(countError('9', 1, 8), INVALID_COUNT);
	assert.equal(countError('999', 1, 8), INVALID_COUNT);
});

test('countError refuses non-integers and blanks', () => {
	assert.equal(countError('1.5', 1, 8), INVALID_COUNT);
	assert.equal(countError('1e1', 1, 8), INVALID_COUNT);
	assert.equal(countError('', 1, 8), INVALID_COUNT);
	assert.equal(countError('abc', 1, 8), INVALID_COUNT);
});

test('countError accepts the number form the input binding delivers', () => {
	// Svelte's number-input binding coerces edits to numbers.
	assert.equal(countError(0, 1, 8), INVALID_COUNT);
	assert.equal(countError(-3, 1, 8), INVALID_COUNT);
	assert.equal(countError(9, 1, 8), INVALID_COUNT);
	assert.equal(countError(1.5, 1, 8), INVALID_COUNT);
	assert.equal(countError(1, 1, 8), null);
	assert.equal(countError(8, 1, 8), null);
	assert.equal(countError(Number.NaN, 1, 8), INVALID_COUNT);
});

test('countError honors the caller limits, not hardcoded ones', () => {
	assert.equal(countError('2', 5, 10), INVALID_COUNT);
	assert.equal(countError('6', 5, 10), null);
	assert.equal(countError('11', 5, 10), INVALID_COUNT);
});

test('seedError accepts null as the random-seed state', () => {
	assert.equal(seedError(null, {}), null);
	assert.equal(seedError(null, { minimum: 100, maximum: 200 }), null);
});

test('seedError accepts a whole number in the default unsigned 32-bit range', () => {
	assert.equal(seedError(0, {}), null);
	assert.equal(seedError(1, {}), null);
	assert.equal(seedError(4294967295, {}), null);
	assert.equal(seedError(123456789, {}), null);
});

test('seedError refuses numbers outside the default range', () => {
	assert.equal(seedError(-1, {}), INVALID_SEED);
	assert.equal(seedError(-0.5, {}), INVALID_SEED);
	assert.equal(seedError(4294967296, {}), INVALID_SEED);
	// A 20-digit value overflows the double's integer precision and the range.
	assert.equal(seedError(Number('12345678901234567890'), {}), INVALID_SEED);
});

test('seedError refuses non-integers and non-finite numbers', () => {
	assert.equal(seedError(1.5, {}), INVALID_SEED);
	assert.equal(seedError(0.1, {}), INVALID_SEED);
	assert.equal(seedError(Number.NaN, {}), INVALID_SEED);
	assert.equal(seedError(Number.POSITIVE_INFINITY, {}), INVALID_SEED);
});

test('seedError uses the manifest bounds when the schema declares them', () => {
	const tight = { minimum: 100, maximum: 200 };
	assert.equal(seedError(99, tight), INVALID_SEED);
	assert.equal(seedError(100, tight), null);
	assert.equal(seedError(150, tight), null);
	assert.equal(seedError(200, tight), null);
	assert.equal(seedError(201, tight), INVALID_SEED);
});

test('seedError fills in a missing bound from the default range', () => {
	assert.equal(seedError(99, { minimum: 100 }), INVALID_SEED);
	assert.equal(seedError(150, { minimum: 100 }), null);
	assert.equal(seedError(4294967296, { minimum: 100 }), INVALID_SEED);
	assert.equal(seedError(1001, { maximum: 1000 }), INVALID_SEED);
	assert.equal(seedError(999, { maximum: 1000 }), null);
	assert.equal(seedError(-1, { maximum: 1000 }), INVALID_SEED);
});
