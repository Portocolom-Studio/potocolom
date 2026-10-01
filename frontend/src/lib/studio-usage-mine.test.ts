import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	byEventsThen,
	categoryShares,
	parseUsageMe,
	sharePct,
	usageMeSearch,
	usageMeWindow,
	type UsageCategoryRow,
	type UsageMe
} from './studio-usage-mine.ts';

const wellFormed: UsageMe = {
	from: '2026-09-01T00:00:00+00:00',
	to: '2026-10-01T00:00:00+00:00',
	totals: { events: 7, gpu_ms: 1200, frames: 7 },
	by_category: [
		{ category: 'art', events: 4 },
		{ category: 'other', events: 3 }
	],
	by_model: [
		{ model_id: 'sd-a', events: 4, avg_gpu_ms: 300, p50_duration_ms: 250.5 },
		{ model_id: 'sd-b', events: 3, avg_gpu_ms: null, p50_duration_ms: null }
	],
	by_model_category: [{ model_id: 'sd-a', category: 'art', events: 4, avg_duration_ms: 900 }]
};

test('the window is the selected range ending now', () => {
	const now = Date.UTC(2026, 9, 1, 12, 0, 0);
	assert.deepEqual(usageMeWindow('30d', now), { from: now - 30 * 24 * 60 * 60 * 1000, to: now });
	assert.deepEqual(usageMeWindow('5m', now), { from: now - 5 * 60 * 1000, to: now });
});

test('the search carries from and to as epoch milliseconds', () => {
	assert.equal(usageMeSearch(1000, 2000), '?from=1000&to=2000');
	assert.equal(usageMeSearch(0, 1), '?from=0&to=1');
});

test('a well-formed usage body parses, including null averages', () => {
	assert.deepEqual(parseUsageMe(wellFormed), wellFormed);
});

test('a body of the wrong shape is refused rather than shown', () => {
	assert.equal(parseUsageMe(null), null);
	assert.equal(parseUsageMe([]), null);
	assert.equal(parseUsageMe({ ...wellFormed, totals: { events: 7 } }), null);
	assert.equal(parseUsageMe({ ...wellFormed, from: 42 }), null);
	assert.equal(
		parseUsageMe({
			...wellFormed,
			by_model: [{ model_id: 'sd-a', events: 4, avg_gpu_ms: 'fast', p50_duration_ms: null }]
		}),
		null
	);
	assert.equal(
		parseUsageMe({ ...wellFormed, by_model_category: [{ model_id: 'sd-a', events: 1 }] }),
		null
	);
});

test('rows sort by events descending, then by their names', () => {
	const rows = [
		{ model_id: 'sd-b', events: 2 },
		{ model_id: 'sd-c', events: 5 },
		{ model_id: 'sd-a', events: 2 }
	];
	assert.deepEqual(
		byEventsThen(rows, (row) => [row.model_id]).map((row) => row.model_id),
		['sd-c', 'sd-a', 'sd-b']
	);
	// The input is not rearranged in place.
	assert.deepEqual(
		rows.map((row) => row.model_id),
		['sd-b', 'sd-c', 'sd-a']
	);
});

test('a two-key tie-break compares model before category', () => {
	const rows = [
		{ model_id: 'sd-b', category: 'art', events: 1 },
		{ model_id: 'sd-a', category: 'other', events: 1 },
		{ model_id: 'sd-a', category: 'art', events: 1 }
	];
	assert.deepEqual(
		byEventsThen(rows, (row) => [row.model_id, row.category]).map(
			(row) => `${row.model_id}:${row.category}`
		),
		['sd-a:art', 'sd-a:other', 'sd-b:art']
	);
});

test('a share is the rounded percentage of the total, and never divided by zero', () => {
	assert.equal(sharePct(1, 3), 33);
	assert.equal(sharePct(2, 3), 67);
	assert.equal(sharePct(0, 3), 0);
	assert.equal(sharePct(5, 0), 0);
});

test('the category mix pairs each count with its share, most used first', () => {
	const rows: UsageCategoryRow[] = [
		{ category: 'other', events: 3 },
		{ category: 'art', events: 4 },
		{ category: 'design', events: 3 }
	];
	assert.deepEqual(categoryShares(rows, 10), [
		{ category: 'art', events: 4, pct: 40 },
		{ category: 'design', events: 3, pct: 30 },
		{ category: 'other', events: 3, pct: 30 }
	]);
	assert.deepEqual(categoryShares(rows, 0), [
		{ category: 'art', events: 4, pct: 0 },
		{ category: 'design', events: 3, pct: 0 },
		{ category: 'other', events: 3, pct: 0 }
	]);
});
