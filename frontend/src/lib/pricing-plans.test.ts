import assert from 'node:assert/strict';
import { test } from 'node:test';
import { planForHours } from './pricing-plans.ts';

test('planForHours picks the cheapest plan whose hours cover the input', () => {
	assert.equal(planForHours(0), 't1');
	assert.equal(planForHours(2.7), 't1');
	assert.equal(planForHours(2.8), 't2');
	assert.equal(planForHours(9.5), 't2');
	assert.equal(planForHours(9.6), 't3');
	assert.equal(planForHours(27), 't3');
	assert.equal(planForHours(27.1), 't4');
});

test('planForHours treats NaN and negative hours as zero', () => {
	assert.equal(planForHours(Number.NaN), 't1');
	assert.equal(planForHours(-1), 't1');
});
