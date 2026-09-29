import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { apiFetch, csrfHeaders, readCsrfToken } from './api.ts';

const globals = globalThis as unknown as Record<string, unknown>;
const saved = new Map<string, unknown>();

function stub(name: string, value: unknown): void {
	if (!saved.has(name)) saved.set(name, globals[name]);
	globals[name] = value;
}

afterEach(() => {
	for (const [name, value] of saved) {
		globals[name] = value;
	}
	saved.clear();
});

test('csrf header is sent when the cookie is present', () => {
	const headers = csrfHeaders('potocolom_csrf=abc123; other=value');
	assert.equal(headers['x-csrf-token'], 'abc123');
});

test('csrf header is omitted when no cookie exists', () => {
	assert.deepEqual(csrfHeaders('other=value'), {});
});

test('host-prefixed csrf cookie is read', () => {
	assert.equal(readCsrfToken('__Host-potocolom_csrf=host-token'), 'host-token');
});

test('apiFetch carries the csrf cookie and credentials into the request', async () => {
	const requests: { url: string; init: RequestInit }[] = [];
	stub('document', { cookie: 'potocolom_csrf=abc123; other=value' });
	stub('fetch', (input: RequestInfo | URL, init: RequestInit = {}) => {
		requests.push({ url: String(input), init });
		return Promise.resolve(new Response('{}'));
	});

	await apiFetch('/api/v1/generations', { method: 'POST' });

	assert.equal(requests.length, 1);
	assert.equal(requests[0].url, '/api/v1/generations');
	assert.equal(requests[0].init.method, 'POST');
	assert.equal(requests[0].init.credentials, 'include');
	assert.equal(new Headers(requests[0].init.headers).get('x-csrf-token'), 'abc123');
});
