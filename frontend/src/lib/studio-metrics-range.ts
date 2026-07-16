export type MetricsRange = '5m' | '1h' | '24h' | '7d' | '30d';

export const METRICS_RANGE_MS: Record<MetricsRange, number> = {
	'5m': 5 * 60 * 1000,
	'1h': 60 * 60 * 1000,
	'24h': 24 * 60 * 60 * 1000,
	'7d': 7 * 24 * 60 * 60 * 1000,
	'30d': 30 * 24 * 60 * 60 * 1000
};

export const METRICS_RANGE_ORDER: MetricsRange[] = ['5m', '1h', '24h', '7d', '30d'];

/** All ranges are available once persisted metrics land (#94). */
export function isMetricsRangeEnabled(_range: MetricsRange): boolean {
	return true;
}
