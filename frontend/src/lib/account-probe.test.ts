import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';

import { probeAccount } from './account-probe.ts';

const accountA = {
	id: '550e8400-e29b-41d4-a716-446655440040',
	email: 'owner@example.com',
	role: 'admin'
};

function jsonResponse(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function mockFetch(
	context: TestContext,
	fetcher: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
): (next: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) => void {
	let current = fetcher;
	context.mock.method(globalThis, 'fetch', (input: RequestInfo | URL, init?: RequestInit) =>
		current(input, init)
	);
	return (next) => {
		current = next;
	};
}

test('account probe returns a validated UUID owner without changing state', async (context) => {
	const requests: string[] = [];
	mockFetch(context, async (input) => {
		requests.push(String(input));
		return jsonResponse(accountA);
	});

	assert.deepEqual(await probeAccount(), {
		kind: 'account',
		current: { email: 'owner@example.com', role: 'admin' },
		ownerId: accountA.id
	});
	assert.deepEqual(requests, ['/api/v1/account']);
});

test('bad UUID keeps a valid display role as unknown, while invalid account data is unknown', async (context) => {
	let body: unknown = { ...accountA, id: 'not-a-uuid' };
	mockFetch(context, async () => jsonResponse(body));
	assert.deepEqual(await probeAccount(), {
		kind: 'unknown',
		current: { email: 'owner@example.com', role: 'admin' }
	});

	body = { email: '', role: 'admin' };
	assert.deepEqual(await probeAccount(), { kind: 'unknown', current: null });
});

test('unreadable JSON and server failures are failed probes', async (context) => {
	const setFetch = mockFetch(context, async () => new Response('<html>', { status: 200 }));
	assert.deepEqual(await probeAccount(), { kind: 'failed' });

	setFetch(async () => jsonResponse({ detail: 'unavailable' }, 503));
	assert.deepEqual(await probeAccount(), { kind: 'failed' });
});

test('401 is signed out; 404 confirms local only from an empty auth method list', async (context) => {
	mockFetch(context, async () => jsonResponse({ detail: 'sign in' }, 401));
	assert.deepEqual(await probeAccount(), { kind: 'signed_out' });

	const requests: string[] = [];
	mockFetch(context, async (input) => {
		requests.push(String(input));
		return String(input) === '/api/v1/account'
			? jsonResponse({ detail: 'local mode' }, 404)
			: jsonResponse({ auth_methods: [] });
	});
	assert.deepEqual(await probeAccount(), { kind: 'local' });
	assert.deepEqual(requests, ['/api/v1/account', '/api/v1/config']);
});

test('404 with configured authentication is unknown; unreadable or failed config is failed', async (context) => {
	const setFetch = mockFetch(context, async (input) =>
		String(input) === '/api/v1/account'
			? jsonResponse({}, 404)
			: jsonResponse({ auth_methods: ['password'] })
	);
	assert.deepEqual(await probeAccount(), { kind: 'unknown', current: null });

	setFetch(async (input) =>
		String(input) === '/api/v1/account'
			? jsonResponse({}, 404)
			: jsonResponse({ detail: 'unavailable' }, 503)
	);
	assert.deepEqual(await probeAccount(), { kind: 'failed' });

	setFetch(async (input) =>
		String(input) === '/api/v1/account'
			? jsonResponse({}, 404)
			: new Response('<html>', { status: 200 })
	);
	assert.deepEqual(await probeAccount(), { kind: 'failed' });
});

test('probe deadline aborts a fetch that does not settle', async (context) => {
	context.mock.timers.enable({ apis: ['setTimeout'] });
	let signal: AbortSignal | undefined;
	mockFetch(context, async (_input, init) => {
		signal = init?.signal ?? undefined;
		return new Promise<Response>(() => {});
	});
	const pending = probeAccount();
	await Promise.resolve();
	context.mock.timers.tick(5_000);
	assert.deepEqual(await pending, { kind: 'failed' });
	assert.equal(signal?.aborted, true);
});

test('route cancellation aborts the request and returns failed immediately', async (context) => {
	let signal: AbortSignal | undefined;
	mockFetch(context, async (_input, init) => {
		signal = init?.signal ?? undefined;
		return new Promise<Response>(() => {});
	});
	const route = new AbortController();
	const pending = probeAccount(route.signal);
	await Promise.resolve();
	route.abort();
	assert.deepEqual(await pending, { kind: 'failed' });
	assert.equal(signal?.aborted, true);
});
