import assert from 'node:assert/strict';
import { test } from 'node:test';

import { fetchSubjectUsage } from './studio-usage-subject.ts';

function answer(status: number, body: unknown = {}): typeof fetch {
	return (async () =>
		new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': 'application/json' }
		})) as typeof fetch;
}

test('a 404 is a missing binding and every other failure is an error', async () => {
	const original = globalThis.fetch;
	try {
		globalThis.fetch = answer(404, { detail: 'Not Found' });
		assert.equal(await fetchSubjectUsage(0, 1), 'unbound');
		globalThis.fetch = answer(503, { detail: 'database unavailable' });
		assert.equal(await fetchSubjectUsage(0, 1), null);
		globalThis.fetch = (async () => {
			throw new TypeError('network');
		}) as typeof fetch;
		assert.equal(await fetchSubjectUsage(0, 1), null);
		const totals = { events: 2, gpu_ms: 10, frames: 3 };
		globalThis.fetch = answer(200, { from: 'a', to: 'b', totals });
		assert.deepEqual(await fetchSubjectUsage(0, 1), { from: 'a', to: 'b', totals });
	} finally {
		globalThis.fetch = original;
	}
});
