// node --test with the built in type stripping, beside the other lib tests
// (see scripts/node-test-loader.mjs). The lasso rules are the part of the
// selection that can look right and still be wrong: a click that masks a
// frame, a cap that truncates the closing stroke, a normalisation that leaves
// the 0..1 range. Each rule is checked against a fixture whose answer is
// arithmetic, not against a copy of the rule.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { lassoToMask, maskOutline, type LassoPoint } from './canvas-selection.ts';

test('a lasso is clamped to the frame and normalised to 0..1', () => {
	// One square overshooting the 512 frame on every side.
	const mask = lassoToMask(
		[
			{ x: -40, y: -40 },
			{ x: 552, y: -40 },
			{ x: 552, y: 552 },
			{ x: -40, y: 552 }
		],
		512,
		512
	);
	assert.ok(mask, 'a frame filling square is a selection');
	const points = mask.polygons[0];
	assert.equal(points.length, 4);
	for (const [x, y] of points) {
		assert.ok(x >= 0 && x <= 1, `x ${x} is outside 0..1`);
		assert.ok(y >= 0 && y <= 1, `y ${y} is outside 0..1`);
	}
	assert.deepEqual(points, [
		[0, 0],
		[1, 0],
		[1, 1],
		[0, 1]
	]);
});

test('points nearer than 3 canvas pixels to the last kept one are dropped', () => {
	const mask = lassoToMask(
		[
			{ x: 20, y: 20 },
			{ x: 480, y: 20 },
			// Exactly 3 px from the last kept point: kept.
			{ x: 483, y: 20 },
			// 1.4 px from it: dropped, and so is everything this close.
			{ x: 484, y: 21 },
			{ x: 484.5, y: 21.5 },
			{ x: 484, y: 480 },
			{ x: 20, y: 480 }
		],
		512,
		512
	);
	assert.ok(mask);
	assert.deepEqual(
		mask.polygons[0],
		[
			[20, 20],
			[480, 20],
			[483, 20],
			[484, 480],
			[20, 480]
		].map(([x, y]) => [x / 512, y / 512]),
		'two points beside the last kept one collapse into it'
	);
});

const SIDE = 480;
const STEP = 3.2;
/** The 600 samples the cap test draws. */
const SAMPLES = (4 * SIDE) / STEP;

/**
 * The perimeter of a 480 square starting at a corner, sampled every 3.2 px:
 * each neighbour pair is one sample apart, so only the cap can change the
 * count. The corners fall on samples (480 / 3.2 is a whole number), which is
 * what keeps the distance across a corner at 3.2 px rather than the shortcut a
 * straddling sample would take.
 */
function perimeterSamples(): LassoPoint[] {
	const points: LassoPoint[] = [];
	for (let sample = 0; sample < SAMPLES; sample += 1) {
		const distance = sample * STEP;
		const edge = Math.floor(distance / SIDE);
		const offset = distance - edge * SIDE;
		const x =
			edge === 0 ? 16 + offset : edge === 1 ? 16 + SIDE : edge === 2 ? 16 + SIDE - offset : 16;
		const y =
			edge === 0 ? 16 : edge === 1 ? 16 + offset : edge === 2 ? 16 + SIDE : 16 + SIDE - offset;
		points.push({ x, y });
	}
	return points;
}

/** Where a point sits along that perimeter, by the edge it lies on. */
function distanceAlong(point: LassoPoint): number {
	if (point.y === 16) return point.x - 16;
	if (point.x === 496) return 480 + (point.y - 16);
	if (point.y === 496) return 960 + (496 - point.x);
	return 1440 + (496 - point.y);
}

test('a drag denser than 512 points keeps an evenly spaced 512', () => {
	const points = perimeterSamples();
	assert.equal(points.length, 600, 'the fixture must overshoot the cap');

	const mask = lassoToMask(points, 512, 512);
	assert.ok(mask);
	const outline = maskOutline(mask, 512, 512);
	assert.equal(outline.length, 512, 'exactly the cap, not the whole drag');

	// Spread over the whole path: sample i lands within one sample of the
	// position i / 511 of the way along it. Truncating to the first 512 would
	// stop 280 px short of the closing point; sampling only the head would
	// leave the same hole.
	const distances = outline.map(distanceAlong);
	const span = distances[distances.length - 1];
	assert.ok(Math.abs(span - 1916.8) < 1e-6, `the closing point survives, got ${span}`);
	for (let index = 0; index < distances.length; index += 1) {
		const ideal = (index * span) / (distances.length - 1);
		assert.ok(
			Math.abs(distances[index] - ideal) <= 2,
			`point ${index} sits at ${distances[index]} px, ideal ${ideal}`
		);
	}
});

test('a click produces no selection', () => {
	// The browser reports the same point twice on a press and a release.
	assert.equal(
		lassoToMask(
			[
				{ x: 200, y: 200 },
				{ x: 201, y: 201 }
			],
			512,
			512
		),
		null
	);
	// Three records, all within the 3 px spacing: one point survived.
	assert.equal(
		lassoToMask(
			[
				{ x: 100, y: 100 },
				{ x: 101, y: 101 },
				{ x: 102, y: 102 }
			],
			512,
			512
		),
		null
	);
	// Two points far apart still describe a line, not an enclosed area.
	assert.equal(
		lassoToMask(
			[
				{ x: 0, y: 0 },
				{ x: 512, y: 512 }
			],
			512,
			512
		),
		null
	);
});

/**
 * A triangle on the base y = 0 whose apex sits at (12, rise * 500 / 512), so
 * its area is 256 * rise * 500 / 512 = 250 * rise square pixels: under the
 * one percent line (2621.44 px2 of a 512 frame) at rise 10, over it at 11.
 * Both drags keep every point, so the only difference between them is area.
 */
function sliver(rise: number): LassoPoint[] {
	const points: LassoPoint[] = [
		{ x: 0, y: 0 },
		{ x: 512, y: 0 }
	];
	for (let step = 20; step <= 500; step += 20) {
		points.push({ x: 512 - step, y: (step * rise) / 512 });
	}
	return points;
}

test('a scribble is no selection until it encloses one percent of the frame', () => {
	assert.equal(lassoToMask(sliver(10), 512, 512), null, '2500 px2 is below one percent');
	const mask = lassoToMask(sliver(11), 512, 512);
	assert.ok(mask, '2750 px2 clears one percent');
	assert.equal(mask.polygons[0].length, sliver(11).length, 'the area rule alone decided this');
});

test('maskOutline puts the lasso back on the canvas it came from', () => {
	const source: LassoPoint[] = [
		{ x: 32, y: 64 },
		{ x: 400, y: 48 },
		{ x: 448, y: 400 },
		{ x: 96, y: 460 }
	];
	const mask = lassoToMask(source, 512, 512);
	assert.ok(mask);
	assert.deepEqual(
		mask.polygons[0],
		source.map((point) => [point.x / 512, point.y / 512]),
		'normalised, so it can travel over the wire'
	);
	assert.deepEqual(maskOutline(mask, 512, 512), source, 'a round trip lands on the same pixels');
	assert.deepEqual(maskOutline(mask, 1024, 768), [
		{ x: 64, y: 96 },
		{ x: 800, y: 72 },
		{ x: 896, y: 600 },
		{ x: 192, y: 690 }
	]);
	assert.deepEqual(maskOutline({ polygons: [] }, 512, 512), [], 'no polygon, no outline');
});
