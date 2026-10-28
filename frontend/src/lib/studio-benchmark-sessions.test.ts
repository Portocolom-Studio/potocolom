import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	BENCHMARK_REPORT_CACHE_LIMIT,
	loadBenchmarkSessions,
	mergeBenchmarkSessions,
	retainBenchmarkReports,
	type BenchmarkSession
} from './studio-benchmark-sessions.ts';

function session(id: string, createdAt: string, report = false): BenchmarkSession {
	return {
		id,
		label: id,
		createdAt,
		report: report
			? {
					created_at: createdAt,
					prompt_count: 0,
					models: [],
					variants_per_prompt: 0,
					total_jobs: 0,
					succeeded: 0,
					failed: 0,
					model_stats: [],
					results: []
				}
			: null
	};
}

test('report retention keeps the selected report and bounds other report bodies', () => {
	const sessions = Array.from({ length: 8 }, (_, index) =>
		session(`s${index}`, `2026-10-${String(index + 1).padStart(2, '0')}T00:00:00Z`, true)
	);
	const retained = retainBenchmarkReports(sessions, 's0');
	assert.equal(
		retained.filter((entry) => entry.report !== null).length,
		BENCHMARK_REPORT_CACHE_LIMIT
	);
	assert.ok(retained.find((entry) => entry.id === 's0')?.report);
	assert.ok(retained.find((entry) => entry.id === 's7')?.report);
	assert.equal(retained.find((entry) => entry.id === 's3')?.report, null);
});

test('refresh merge keeps the selected session across reordering and carries its report', () => {
	const previous = [session('older', '2026-10-01', true), session('selected', '2026-10-02', true)];
	const loaded = [session('newest', '2026-10-03'), session('selected', '2026-10-02')];
	const merged = mergeBenchmarkSessions(loaded, previous, 'selected');
	assert.equal(merged.selectedSessionId, 'selected');
	assert.deepEqual(
		merged.sessions.map((entry) => [entry.id, entry.report !== null]),
		[
			['newest', false],
			['selected', true]
		]
	);
});

test('refresh falls back once when the saved session was removed', () => {
	const merged = mergeBenchmarkSessions(
		[session('newest', '2026-10-03'), session('next', '2026-10-02')],
		[session('deleted', '2026-10-01', true)],
		'deleted'
	);
	assert.equal(merged.selectedSessionId, 'newest');
	assert.deepEqual(
		merged.sessions.map((entry) => entry.id),
		['newest', 'next']
	);
});

test('a failed warm list refresh does not fall through to a different static report', async () => {
	const originalFetch = globalThis.fetch;
	const requests: string[] = [];
	try {
		globalThis.fetch = (async (input) => {
			const url = String(input);
			requests.push(url);
			return new Response('{}', { status: url === '/api/v1/benchmark/sessions' ? 503 : 200 });
		}) as typeof fetch;

		assert.deepEqual(
			await loadBenchmarkSessions(true, undefined, { fallbackToStaticOnApiFailure: false }),
			[]
		);
		assert.deepEqual(requests, ['/api/v1/benchmark/sessions']);
	} finally {
		globalThis.fetch = originalFetch;
	}
});
