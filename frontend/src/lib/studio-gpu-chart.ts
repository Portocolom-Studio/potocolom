import type { MetricsRange } from '$lib/studio-metrics-range';

export type ChartPoint = { ts: number; value: number };

export type ChartMargins = {
	top: number;
	right: number;
	bottom: number;
	left: number;
};

export const CHART_MARGINS: ChartMargins = { top: 20, right: 52, bottom: 30, left: 44 };

/** Fixed entity colors - never cycle by visibility order. */
export const GPU_METRIC_COLORS = {
	compute: 'var(--chart-1)',
	vram: 'var(--chart-2)',
	temperature: 'var(--chart-3)',
	power: 'var(--chart-4)'
} as const;

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

export function rollingMean(points: ChartPoint[], window: number): ChartPoint[] {
	if (points.length === 0 || window < 1) return [];
	return points.map((point, index) => {
		const start = Math.max(0, index - window + 1);
		const slice = points.slice(start, index + 1);
		const avg = slice.reduce((sum, entry) => sum + entry.value, 0) / slice.length;
		return { ts: point.ts, value: avg };
	});
}

export function nearestPointIndex(points: ChartPoint[], targetTs: number): number {
	if (points.length === 0) return -1;
	if (points.length === 1) return 0;
	let lo = 0;
	let hi = points.length - 1;
	while (lo < hi) {
		const mid = Math.floor((lo + hi) / 2);
		if (points[mid].ts < targetTs) lo = mid + 1;
		else hi = mid;
	}
	if (lo > 0) {
		const prev = lo - 1;
		return Math.abs(points[prev].ts - targetTs) <= Math.abs(points[lo].ts - targetTs) ? prev : lo;
	}
	return lo;
}

export function tsFromPlotX(
	x: number,
	windowStartMs: number,
	windowEndMs: number,
	plotLeft: number,
	plotWidth: number
): number {
	const span = Math.max(windowEndMs - windowStartMs, 1);
	const ratio = Math.max(0, Math.min(1, (x - plotLeft) / Math.max(plotWidth, 1)));
	return windowStartMs + ratio * span;
}

export function timeTicks(windowStartMs: number, windowEndMs: number, count: number): number[] {
	if (count < 2) return [windowStartMs, windowEndMs];
	const span = windowEndMs - windowStartMs;
	return Array.from({ length: count }, (_, index) => windowStartMs + (span * index) / (count - 1));
}

export function formatTimeTick(ts: number, range: MetricsRange = '5m'): string {
	const date = new Date(ts);
	if (range === '5m') {
		return date.toLocaleTimeString(undefined, {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}
	if (range === '1h' || range === '24h') {
		return date.toLocaleTimeString(undefined, {
			hour: '2-digit',
			minute: '2-digit'
		});
	}
	if (range === '7d') {
		return date.toLocaleDateString(undefined, {
			weekday: 'short',
			day: 'numeric'
		});
	}
	return date.toLocaleDateString(undefined, {
		month: 'short',
		day: 'numeric'
	});
}

function formatClock(ts: number, withSeconds: boolean): string {
	return new Date(ts).toLocaleTimeString(undefined, {
		hour: '2-digit',
		minute: '2-digit',
		...(withSeconds ? { second: '2-digit' } : {})
	});
}

export function formatRangeCaption(
	windowStartMs: number,
	windowEndMs: number,
	range: MetricsRange = '5m'
): string {
	const start = new Date(windowStartMs);
	const end = new Date(windowEndMs);
	const withSeconds = range === '5m';
	const sameDay = start.toDateString() === end.toDateString();
	const datePart = start.toLocaleDateString(undefined, {
		day: 'numeric',
		month: 'short',
		year: 'numeric'
	});
	if (sameDay) {
		return `${datePart}, ${formatClock(windowStartMs, withSeconds)}-${formatClock(windowEndMs, withSeconds)} (local)`;
	}
	const endDate = end.toLocaleDateString(undefined, {
		day: 'numeric',
		month: 'short',
		year: 'numeric'
	});
	return `${datePart} ${formatClock(windowStartMs, withSeconds)} - ${endDate} ${formatClock(windowEndMs, withSeconds)} (local)`;
}

export function latestPoint(points: ChartPoint[]): ChartPoint | null {
	if (points.length === 0) return null;
	return points[points.length - 1];
}

export function formatMetricValue(value: number, unit: string): string {
	if (unit === '%') return `${Math.round(value)}%`;
	return `${value.toFixed(1)}${unit}`;
}
