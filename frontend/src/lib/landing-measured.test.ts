import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { measured } from './landing-measured.ts';

type BenchmarkReport = {
	total_jobs: number;
	conditions: { gpu: string };
	model_stats: { model_id: string; avg_gpu_ms: number | null }[];
};

const report: BenchmarkReport = JSON.parse(
	readFileSync(new URL('../../static/benchmark/results.json', import.meta.url), 'utf8')
);

test('landing numbers match the public benchmark report', () => {
	assert.equal(measured.totalJobs, report.total_jobs);
	assert.equal(measured.gpu, report.conditions.gpu);

	const seconds = measured.rows.map((row) => row.seconds);
	assert.deepEqual(
		seconds,
		[...seconds].sort((a, b) => a - b),
		'rows must run fastest first'
	);

	for (const row of measured.rows) {
		const stats = report.model_stats.find((entry) => entry.model_id === row.id);
		if (!stats || stats.avg_gpu_ms === null) assert.fail(`${row.id} has no timing in the report`);
		assert.equal(Math.round(stats.avg_gpu_ms / 100) / 10, row.seconds, row.id);
	}
});
