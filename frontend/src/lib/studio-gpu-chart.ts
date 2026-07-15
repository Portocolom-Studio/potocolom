export type ChartPoint = { ts: number; value: number };

export type ChartMargins = {
	top: number;
	right: number;
	bottom: number;
	left: number;
};

export const CHART_MARGINS: ChartMargins = { top: 14, right: 52, bottom: 30, left: 44 };

export function xScale(
	ts: number,
	windowStartMs: number,
	windowEndMs: number,
	plotLeft: number,
	plotWidth: number
): number {
	const span = Math.max(windowEndMs - windowStartMs, 1);
	return plotLeft + ((ts - windowStartMs) / span) * plotWidth;
}

export function yScale(value: number, plotTop: number, plotHeight: number, max = 100): number {
	const clamped = Math.max(0, Math.min(max, value));
	return plotTop + plotHeight - (clamped / max) * plotHeight;
}

export function linePath(
	points: ChartPoint[],
	windowStartMs: number,
	windowEndMs: number,
	plotLeft: number,
	plotWidth: number,
	plotTop: number,
	plotHeight: number,
	maxY = 100
): string {
	if (points.length === 0) return '';
	return points
		.map((point, index) => {
			const x = xScale(point.ts, windowStartMs, windowEndMs, plotLeft, plotWidth);
			const y = yScale(point.value, plotTop, plotHeight, maxY);
			return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)},${y.toFixed(2)}`;
		})
		.join(' ');
}

export function areaPath(
	points: ChartPoint[],
	windowStartMs: number,
	windowEndMs: number,
	plotLeft: number,
	plotWidth: number,
	plotTop: number,
	plotHeight: number,
	maxY = 100
): string {
	if (points.length === 0) return '';
	const baseY = plotTop + plotHeight;
	const line = linePath(
		points,
		windowStartMs,
		windowEndMs,
		plotLeft,
		plotWidth,
		plotTop,
		plotHeight,
		maxY
	);
	const firstX = xScale(points[0].ts, windowStartMs, windowEndMs, plotLeft, plotWidth);
	const lastX = xScale(
		points[points.length - 1].ts,
		windowStartMs,
		windowEndMs,
		plotLeft,
		plotWidth
	);
	return `${line} L${lastX.toFixed(2)},${baseY.toFixed(2)} L${firstX.toFixed(2)},${baseY.toFixed(2)} Z`;
}

export function timeTicks(windowStartMs: number, windowEndMs: number, count: number): number[] {
	if (count < 2) return [windowStartMs, windowEndMs];
	const span = windowEndMs - windowStartMs;
	return Array.from({ length: count }, (_, index) => windowStartMs + (span * index) / (count - 1));
}

export function formatTimeTick(ts: number): string {
	return new Date(ts).toLocaleTimeString(undefined, {
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit'
	});
}

export function latestPoint(points: ChartPoint[]): ChartPoint | null {
	if (points.length === 0) return null;
	return points[points.length - 1];
}
