import assert from 'node:assert/strict';
import { test } from 'node:test';
import { initialAuthTab, joinWaitlist } from './waitlist.ts';

function reply(status: number, body: unknown, contentType = 'application/json'): typeof fetch {
	return (async () =>
		new Response(JSON.stringify(body), {
			status,
			headers: { 'content-type': contentType }
		})) as typeof fetch;
}

test('joinWaitlist posts the address and locale as JSON', async () => {
	let sent: RequestInit | undefined;
	let url = '';
	const fetcher = (async (input: string | URL | Request, init?: RequestInit) => {
		url = String(input);
		sent = init;
		return new Response('{"status":"ok"}', { headers: { 'content-type': 'application/json' } });
	}) as typeof fetch;
	assert.equal(await joinWaitlist('/api/waitlist', 'a@b.test', 'es', fetcher), 'done');
	assert.equal(url, '/api/waitlist');
	assert.equal(sent?.method, 'POST');
	assert.deepEqual(JSON.parse(String(sent?.body)), { email: 'a@b.test', locale: 'es' });
});

test('joinWaitlist reports an address already on the list', async () => {
	assert.equal(
		await joinWaitlist('/w', 'a@b.test', 'en', reply(200, { status: 'exists' })),
		'already'
	);
});

test('joinWaitlist treats errors, non-JSON answers and network failures as errors', async () => {
	assert.equal(await joinWaitlist('/w', 'a@b.test', 'en', reply(500, {})), 'error');
	assert.equal(await joinWaitlist('/w', 'a@b.test', 'en', reply(200, {}, 'text/html')), 'error');
	const offline = (async () => {
		throw new TypeError('offline');
	}) as typeof fetch;
	assert.equal(await joinWaitlist('/w', 'a@b.test', 'en', offline), 'error');
});

test('initialAuthTab opens the waitlist only when asked and available', () => {
	assert.equal(initialAuthTab('?tab=waitlist', true), 'waitlist');
	assert.equal(initialAuthTab('?tab=waitlist', false), 'signin');
	assert.equal(initialAuthTab('', true), 'signin');
	assert.equal(initialAuthTab('?tab=other', true), 'signin');
});

test('initialAuthTab leads with the waitlist on the landing build', () => {
	assert.equal(initialAuthTab('', true, true), 'waitlist');
	assert.equal(initialAuthTab('?tab=signin', true, true), 'signin');
	assert.equal(initialAuthTab('', false, true), 'signin');
});
