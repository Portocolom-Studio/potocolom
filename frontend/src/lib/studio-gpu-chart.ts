import type { MetricsRange } from '$lib/studio-metrics-range';

export type ChartPoint = { ts: number; value: number };

export type ChartBandPoint = { ts: number; min: number; max: number; mean: number };

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
