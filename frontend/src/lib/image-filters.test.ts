import assert from 'node:assert/strict';
import test from 'node:test';
import type { Generation } from './studio.svelte.ts';
import {
	clearImageFilters,
	customDayBounds,
	customRangeReversed,
	defaultImageFilters,
	generationMatchesImageFilters,
	imageFiltersActive,
	imageFiltersEqual,
	localDayStart,
	modelOptionLabel,
	presetDayRange,
	withImageFilters
} from './image-filters.ts';
import type { ImageSizeBin } from './image-filters.ts';

// The helpers build windows in the browser's own timezone. Madrid has both a
// DST spring forward (2026-03-29) and a fall back (2026-10-25), so a fixed
// offset zone would hide exactly the bugs these tests exist to catch.
process.env.TZ = 'Europe/Madrid';

function filters(overrides: Partial<typeof defaultImageFilters> = {}) {
	return { ...defaultImageFilters, ...overrides };
}

function generation(
	model_id: string,
	created_at: string,
	dimensions: Array<{ width: number; height: number }> = []
): Pick<Generation, 'model_id' | 'created_at' | 'assets'> {
	return {
		model_id,
		created_at,
		assets: dimensions.map((asset, index) => ({
			id: `asset-${index}`,
			url: `/asset-${index}.webp`,
			thumbnail_url: null,
			download_url: `/asset-${index}.webp`,
			...asset
		}))
	};
}

test('an untouched filter set is inactive and equals the default', () => {
	assert.equal(imageFiltersActive(defaultImageFilters), false);
	assert.equal(imageFiltersActive(filters({ models: ['a'] })), true);
	assert.equal(imageFiltersActive(filters({ range: 'today' })), true);
	assert.equal(imageFiltersActive(filters({ sizes: ['512'] })), true);
	assert.equal(imageFiltersEqual(defaultImageFilters, filters()), true);
	// Selection order is not part of the selection.
	assert.equal(
		imageFiltersEqual(filters({ models: ['a', 'b'] }), filters({ models: ['b', 'a'] })),
		true
	);
	assert.equal(
		imageFiltersEqual(filters({ models: ['a'] }), filters({ models: ['a', 'b'] })),
		false
	);
	assert.equal(imageFiltersEqual(filters({ range: 'today' }), filters()), false);
	assert.equal(
		imageFiltersEqual(filters({ sizes: ['512', '1k'] }), filters({ sizes: ['1k', '512'] })),
		true
	);
	assert.equal(
		imageFiltersEqual(filters({ sizes: ['512', '512'] }), filters({ sizes: ['512'] })),
		true
	);
	assert.equal(imageFiltersEqual(filters({ sizes: ['512'] }), filters({ sizes: ['768'] })), false);
});

test('Clear resets size, model, and date filters to fresh defaults', () => {
	const cleared = clearImageFilters();
	assert.deepEqual(cleared, { models: [], sizes: [], range: 'all', from: '', through: '' });
	assert.equal(imageFiltersActive(cleared), false);
	assert.equal(imageFiltersEqual(cleared, defaultImageFilters), true);
	cleared.models.push('changed');
	cleared.sizes.push('512');
	assert.deepEqual(clearImageFilters(), {
		models: [],
		sizes: [],
		range: 'all',
		from: '',
		through: ''
	});
});

test('size bins classify portrait and landscape masters at every exact boundary', () => {
	const cases: Array<{ width: number; height: number; size: ImageSizeBin }> = [
		{ width: 1, height: 1, size: '512' },
		{ width: 639, height: 200, size: '512' },
		{ width: 640, height: 1, size: '768' },
		{ width: 300, height: 895, size: '768' },
		{ width: 896, height: 300, size: '1k' },
		{ width: 1216, height: 1344, size: '1k' },
		{ width: 1535, height: 896, size: '1k' },
		{ width: 1536, height: 768, size: '2k' },
		{ width: 1920, height: 2560, size: '2k' },
		{ width: 3071, height: 1536, size: '2k' },
		{ width: 3072, height: 1536, size: '4k' },
		{ width: 3840, height: 2160, size: '4k' },
		{ width: 4096, height: 3072, size: '4k' },
		{ width: 6143, height: 4096, size: '4k' },
		{ width: 6144, height: 4096, size: 'above_4k' }
	];
	for (const { width, height, size } of cases) {
		assert.equal(
			generationMatchesImageFilters(
				generation('model', 'not-a-date', [{ width, height }]),
				filters({ sizes: [size] })
			),
			true,
			`${width}x${height} should match ${size}`
		);
	}
});

test('size matching is OR across master outputs and empty or invalid assets are unknown', () => {
	const now = '2026-06-15T12:00:00Z';
	for (const [width, height] of [
		[512, 0],
		[0, 512],
		[-1, 512],
		[512, -1],
		[Infinity, 512],
		[512, Infinity],
		[NaN, 512],
		[512, NaN]
	]) {
		assert.equal(
			generationMatchesImageFilters(
				generation('m', now, [{ width, height }]),
				filters({ sizes: ['unknown'] })
			),
			true,
			`${width}x${height} has no usable dimensions`
		);
	}
	assert.equal(
		generationMatchesImageFilters(
			generation('m', now, [
				{ width: 512, height: 512 },
				{ width: 2560, height: 1920 }
			]),
			filters({ sizes: ['2k'] })
		),
		true
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('m', now, [{ width: 512, height: 512 }]),
			filters({ sizes: ['512', '4k'] })
		),
		true
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('m', now, [{ width: 0, height: 512 }]),
			filters({ sizes: ['unknown'] })
		),
		true
	);
	assert.equal(
		generationMatchesImageFilters(generation('m', now), filters({ sizes: ['unknown'] })),
		true
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('m', now, [{ width: 512, height: 512 }]),
			filters({ sizes: ['unknown'] })
		),
		false
	);
});

test('size filters OR together and AND with model and date filters', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const selected = filters({ models: ['keep'], sizes: ['2k', '4k'], range: 'today' });
	assert.equal(
		generationMatchesImageFilters(
			generation('keep', now.toISOString(), [{ width: 2560, height: 1920 }]),
			selected,
			now
		),
		true
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('keep', now.toISOString(), [{ width: 512, height: 512 }]),
			selected,
			now
		),
		false
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('other', now.toISOString(), [{ width: 2560, height: 1920 }]),
			selected,
			now
		),
		false
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('keep', '2026-06-14T21:59:59.999Z', [{ width: 2560, height: 1920 }]),
			selected,
			now
		),
		false
	);
});

test('a local day starts at local midnight, not at a UTC instant', () => {
	const start = localDayStart('2026-06-15');
	assert.ok(start);
	// Madrid is UTC+2 in June, so local midnight is 22:00 of the day before.
	assert.equal(start.getFullYear(), 2026);
	assert.equal(start.getMonth(), 5);
	assert.equal(start.getDate(), 15);
	assert.equal(start.getHours(), 0);
	assert.equal(localDayStart('2026-02-30'), null);
	assert.equal(localDayStart('15-06-2026'), null);
	assert.equal(localDayStart(''), null);
});

test('a custom range runs from the first local midnight to the one after Through', () => {
	const window = customDayBounds(
		filters({ range: 'custom', from: '2026-06-15', through: '2026-06-16' })
	);
	assert.ok(window);
	assert.equal(window.from.getDate(), 15);
	assert.equal(window.from.getHours(), 0);
	// Through is inclusive, so the window stops at the start of the 17th.
	assert.equal(window.throughExclusive.getDate(), 17);
	assert.equal(window.throughExclusive.getHours(), 0);
	// One selected day is a whole day, not an empty window.
	const single = customDayBounds(
		filters({ range: 'custom', from: '2026-06-15', through: '2026-06-15' })
	);
	assert.ok(single);
	assert.equal(single.throughExclusive.getTime() - single.from.getTime(), 24 * 60 * 60 * 1000);
});

test('a custom window stays a whole day across the DST spring forward', () => {
	// 2026-03-29 is 23 hours long in Madrid: clocks jump 02:00 to 03:00.
	const window = customDayBounds(
		filters({ range: 'custom', from: '2026-03-29', through: '2026-03-29' })
	);
	assert.ok(window);
	assert.equal(window.from.getHours(), 0);
	assert.equal(window.throughExclusive.getHours(), 0);
	assert.equal(window.throughExclusive.getDate(), 30);
	assert.equal(window.throughExclusive.getTime() - window.from.getTime(), 23 * 60 * 60 * 1000);
});

test('a custom window stays a whole day across the DST fall back', () => {
	// 2026-10-25 is 25 hours long in Madrid.
	const window = customDayBounds(
		filters({ range: 'custom', from: '2026-10-25', through: '2026-10-25' })
	);
	assert.ok(window);
	assert.equal(window.throughExclusive.getTime() - window.from.getTime(), 25 * 60 * 60 * 1000);
});

test('an incomplete or reversed custom range is reversed only when it really is', () => {
	assert.equal(
		customRangeReversed(filters({ range: 'custom', from: '2026-06-16', through: '2026-06-15' })),
		true
	);
	assert.equal(
		customRangeReversed(filters({ range: 'custom', from: '2026-06-15', through: '2026-06-15' })),
		false
	);
	assert.equal(
		customRangeReversed(filters({ range: 'custom', from: '2026-06-15', through: '2026-06-16' })),
		false
	);
	// A missing or bogus bound is not a reversed range: it just builds nothing.
	assert.equal(
		customRangeReversed(filters({ range: 'custom', from: '2026-06-15', through: '' })),
		false
	);
	assert.equal(
		customRangeReversed(filters({ range: 'custom', from: 'nonsense', through: '2026-06-15' })),
		false
	);
	assert.equal(customRangeReversed(filters()), false);
});

test('customDayBounds refuses a reversed calendar window directly', () => {
	assert.equal(
		customDayBounds(filters({ range: 'custom', from: '2026-06-16', through: '2026-06-15' })),
		null
	);
});

test('presets cover whole local days ending today', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const today = presetDayRange('today', now);
	assert.ok(today);
	assert.equal(today.from.getDate(), 15);
	assert.equal(today.from.getHours(), 0);
	assert.equal(today.throughExclusive.getDate(), 16);
	const last7 = presetDayRange('last7', now);
	assert.ok(last7);
	assert.equal(last7.from.getDate(), 9);
	assert.equal(last7.throughExclusive.getDate(), 16);
	const last30 = presetDayRange('last30', now);
	assert.ok(last30);
	assert.equal(last30.from.getDate(), 17);
	assert.equal(last30.from.getMonth(), 4);
	assert.equal(last30.throughExclusive.getDate(), 16);
	assert.equal(presetDayRange('all', now), null);
	assert.equal(presetDayRange('custom', now), null);
});

test('presets span the DST fall back without drifting an hour', () => {
	// 2026-10-25 is the fall back; a window covering it still ends at
	// local midnight of the next day, not at 23:00 or 01:00.
	const now = new Date(2026, 9, 26, 9, 0);
	const last7 = presetDayRange('last7', now);
	assert.ok(last7);
	assert.equal(last7.throughExclusive.getDate(), 27);
	assert.equal(last7.throughExclusive.getHours(), 0);
	assert.equal(last7.from.getDate(), 20);
});

test('the query carries repeated model and deduplicated size keys with the window as instants', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const query = withImageFilters(
		new URLSearchParams('roots_only=true'),
		filters({ models: ['a', 'b'], sizes: ['512', '2k', '512'], range: 'last7' }),
		now
	);
	assert.ok(query);
	assert.equal(query.get('roots_only'), 'true');
	assert.deepEqual(query.getAll('model_id'), ['a', 'b']);
	assert.deepEqual(query.getAll('size'), ['512', '2k']);
	const from = new Date(2026, 5, 9);
	const through = new Date(2026, 5, 16);
	assert.equal(query.get('created_from'), from.toISOString());
	assert.equal(query.get('created_before'), through.toISOString());
});

test('an untouched filter set sends no filter keys at all', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const query = withImageFilters(new URLSearchParams('roots_only=true'), defaultImageFilters, now);
	assert.ok(query);
	assert.deepEqual(query.getAll('model_id'), []);
	assert.deepEqual(query.getAll('size'), []);
	assert.equal(query.get('created_from'), null);
	assert.equal(query.get('created_before'), null);
});

test('a reversed or incomplete custom range builds no query', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	assert.equal(
		withImageFilters(
			new URLSearchParams(),
			filters({ range: 'custom', from: '2026-06-16', through: '2026-06-15' }),
			now
		),
		null
	);
	assert.equal(
		withImageFilters(
			new URLSearchParams(),
			filters({ models: ['m'], sizes: ['1k'], range: 'custom', from: '2026-06-15', through: '' }),
			now
		),
		null
	);
	const unchanged = filters({
		models: ['m'],
		sizes: ['1k'],
		range: 'custom',
		from: '2026-06-15',
		through: ''
	});
	assert.deepEqual(unchanged, {
		models: ['m'],
		sizes: ['1k'],
		range: 'custom',
		from: '2026-06-15',
		through: ''
	});
});

test('the local matching rules agree with the window it would send', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const last7 = filters({ range: 'last7' });
	assert.equal(
		generationMatchesImageFilters(
			generation('a', new Date(2026, 5, 9, 0, 0).toISOString()),
			last7,
			now
		),
		true
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('a', new Date(2026, 5, 8, 23, 59).toISOString()),
			last7,
			now
		),
		false
	);
	// The exclusive end: exactly the window's end is already tomorrow.
	assert.equal(
		generationMatchesImageFilters(
			generation('a', new Date(2026, 5, 16, 0, 0).toISOString()),
			last7,
			now
		),
		false
	);
	assert.equal(
		generationMatchesImageFilters(
			generation('a', new Date(2026, 5, 15, 23, 30).toISOString()),
			last7,
			now
		),
		true
	);
});

test('the local matching rules honour the model selection', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const modelled = filters({ models: ['keep'] });
	assert.equal(
		generationMatchesImageFilters(generation('keep', now.toISOString()), modelled, now),
		true
	);
	assert.equal(
		generationMatchesImageFilters(generation('other', now.toISOString()), modelled, now),
		false
	);
	// No selection matches everything, including an unparseable date.
	assert.equal(
		generationMatchesImageFilters(generation('x', 'not a date'), defaultImageFilters, now),
		true
	);
	// A date window still refuses a row it cannot place.
	assert.equal(
		generationMatchesImageFilters(generation('x', 'not a date'), filters({ range: 'last30' }), now),
		false
	);
});

test('a custom range matches inside its own bounds only', () => {
	const now = new Date(2026, 5, 15, 13, 45);
	const custom = filters({ range: 'custom', from: '2026-06-10', through: '2026-06-12' });
	const matches = (iso: string) => generationMatchesImageFilters(generation('a', iso), custom, now);
	assert.equal(matches(new Date(2026, 5, 10, 0, 0).toISOString()), true);
	assert.equal(matches(new Date(2026, 5, 12, 23, 59).toISOString()), true);
	assert.equal(matches(new Date(2026, 5, 13, 0, 0).toISOString()), false);
	assert.equal(matches(new Date(2026, 5, 9, 23, 59).toISOString()), false);
});

test('an option label never comes out empty', () => {
	assert.equal(modelOptionLabel({ id: 'z-image', name: 'Z-Image Turbo' }), 'Z-Image Turbo');
	assert.equal(modelOptionLabel({ id: 'z-image', name: '' }), 'z-image');
	assert.equal(modelOptionLabel(undefined), '');
});
