// The lasso behind the realtime canvas selection (issue #376). Pure geometry
// on plain points, so node --test can hold the rules the panel applies without
// a DOM: what counts as a selection, how coarse it is sent, how it comes back
// for the outline, and how an edit prompt rides on it. The panel owns the
// pointers and the session.

import type { RealtimeCanvasMask } from './realtime-canvas';

/** A lasso point in canvas pixels. */
export type LassoPoint = { x: number; y: number };

/**
 * Points closer together than this collapse into the last kept one. A drag
 * records every pointermove it sees, which on a trackpad is dozens of points
 * per centimetre; the wire does not need them and the worker would resample
 * them anyway. Canvas pixels, so the rule does not change with the frame.
 */
const MIN_SPACING_PX = 3;

/** The most points one polygon may carry, kept evenly rather than truncated:
 * a lasso's tail is the closing stroke, and dropping it would open the shape. */
const MAX_POINTS = 512;

/**
 * Below this share of the frame there is no selection. A click, or a scribble
 * whose enclosed area is negligible, must not start masking the render.
 */
const MIN_AREA_RATIO = 0.01;

function clamp(value: number, maximum: number): number {
	return Math.min(maximum, Math.max(0, value));
}

/** The enclosed area of a closed polygon, by the shoelace formula. */
function polygonArea(points: LassoPoint[]): number {
	let twice = 0;
	for (let index = 0; index < points.length; index += 1) {
		const next = points[(index + 1) % points.length];
		twice += points[index].x * next.y - next.x * points[index].y;
	}
	return Math.abs(twice) / 2;
}

/** `count` points spread across the whole path, the first and last included. */
function evenSample(points: LassoPoint[], count: number): LassoPoint[] {
	const sampled: LassoPoint[] = [];
	const last = points.length - 1;
	for (let index = 0; index < count; index += 1) {
		sampled.push(points[Math.round((index * last) / (count - 1))]);
	}
	return sampled;
}

/**
 * The mask a drag describes, or null when it describes no selection.
 *
 * The drag's points are clamped to the frame, thinned to the spacing above,
 * capped at MAX_POINTS and normalised to 0..1, in that order: the spacing rule
 * is stated in canvas pixels, so it runs while the points are still pixels.
 * Fewer than three survivors is a click rather than a shape, and an enclosed
 * area under one percent of the frame is a scribble rather than an area; both
 * return null, and the panel keeps whatever selection it already had.
 */
export function lassoToMask(
	points: LassoPoint[],
	width: number,
	height: number
): RealtimeCanvasMask | null {
	const kept: LassoPoint[] = [];
	for (const point of points) {
		const clamped = { x: clamp(point.x, width), y: clamp(point.y, height) };
		const last = kept[kept.length - 1];
		if (last && Math.hypot(clamped.x - last.x, clamped.y - last.y) < MIN_SPACING_PX) continue;
		kept.push(clamped);
	}
	const capped = kept.length > MAX_POINTS ? evenSample(kept, MAX_POINTS) : kept;
	if (capped.length < 3) return null;
	if (polygonArea(capped) < width * height * MIN_AREA_RATIO) return null;
	return { polygons: [capped.map((point) => [point.x / width, point.y / height])] };
}

/**
 * The mask's first polygon back in canvas pixels, to draw its outline. The
 * lasso writes exactly one polygon; a mask with none outlines nothing.
 */
export function maskOutline(mask: RealtimeCanvasMask, width: number, height: number): LassoPoint[] {
	const polygon = mask.polygons[0];
	if (!polygon) return [];
	return polygon.map(([x, y]) => ({ x: x * width, y: y * height }));
}

/**
 * The mask to send for a selection with an edit prompt: the polygons with the
 * text trimmed onto them, or the polygons alone when there is no text. An
 * empty field therefore sends a mask with no `prompt`, which is what sends the
 * worker back to the session prompt inside the selection, and a mask that
 * carried a prompt before does not keep it. The same call carries an applied
 * prompt onto a freshly drawn selection, so the lasso does not silently drop
 * it.
 */
export function maskWithPrompt(
	mask: RealtimeCanvasMask,
	prompt: string | undefined
): RealtimeCanvasMask {
	const trimmed = (prompt ?? '').trim();
	return trimmed === ''
		? { polygons: mask.polygons }
		: { polygons: mask.polygons, prompt: trimmed };
}
