import assert from 'node:assert/strict';
import { test } from 'node:test';

import { gpuHistoryPoints, gpuHistorySearch } from './studio-gpu-history.ts';

test('GPU history query includes the worker filter only when selected', () => {
	assert.equal(
		gpuHistorySearch(1000, 2000, 'auto', 'worker/a'),
		'?from=1000&to=2000&rollup=auto&worker_id=worker%2Fa'
	);
	assert.equal(gpuHistorySearch(1000, 2000, 'raw'), '?from=1000&to=2000&rollup=raw');
});

test('GPU history points retain worker ids and discard invalid timestamps', () => {
	assert.deepEqual(
		gpuHistoryPoints({
			from: '',
			to: '',
			rollup: 'raw',
			samples: [
				{
					ts: '2026-10-01T12:00:00Z',
					worker_id: 'worker-a',
					util_pct: 42,
					vram_used_pct: 61
				},
				{ ts: 'invalid', worker_id: 'worker-b', util_pct: null, vram_used_pct: null }
			]
		}),
		[
			{
				ts: Date.parse('2026-10-01T12:00:00Z'),
				worker_id: 'worker-a',
				util_pct: 42,
				util_min: undefined,
				util_max: undefined,
				vram_used_pct: 61,
				vram_min: undefined,
				vram_max: undefined,
				temperature_c: null,
				power_w: null
			}
		]
	);
});
