import assert from 'node:assert/strict';
import { test } from 'node:test';
import { account } from './account.svelte.ts';
import { watchWorker, worker } from './studio-worker.svelte.ts';

async function settle(): Promise<void> {
	for (let turn = 0; turn < 12; turn += 1) await Promise.resolve();
}

function page(context: import('node:test').TestContext) {
	const events = new EventTarget();
	const stops: Array<() => void> = [];
	Object.defineProperty(events, 'visibilityState', { value: 'visible', writable: true });
	const original = Object.getOwnPropertyDescriptor(globalThis, 'document');
	Object.defineProperty(globalThis, 'document', { configurable: true, value: events });
	context.after(() => {
		for (const stop of stops) stop();
		if (original) Object.defineProperty(globalThis, 'document', original);
		else Reflect.deleteProperty(globalThis, 'document');
		account.current = null;
	});
	return {
		events,
		watch: () => {
			const stop = watchWorker();
			stops.push(stop);
			return stop;
		}
	};
}

test('watchers share polling until the last stop, which resets state and aborts late replies', async (context) => {
	const { events, watch } = page(context);
	context.mock.timers.enable({ apis: ['setInterval'] });
	const signals: AbortSignal[] = [];
	let answer: Response | Promise<Response> = Response.json({ gpu: { device: 'test-gpu' } });
	const fetcher = context.mock.method(
		globalThis,
		'fetch',
		async (input: RequestInfo | URL, init?: RequestInit) => {
			assert.equal(input, '/api/v1/studio/gpu');
			assert.ok(init?.signal instanceof AbortSignal);
			signals.push(init.signal);
			return answer;
		}
	);
	const stopFirst = watch();
	const stopSecond = watch();
	await settle();
	assert.equal(fetcher.mock.callCount(), 1);
	assert.equal(worker.online, true);
	assert.equal(worker.gpu?.device, 'test-gpu');

	answer = new Response(null, { status: 500 });
	context.mock.timers.tick(15000);
	await settle();
	assert.equal(fetcher.mock.callCount(), 2);
	assert.equal(worker.online, true, 'a non-503 error keeps the last answer');

	stopFirst();
	stopFirst();
	answer = new Response(null, { status: 503 });
	events.dispatchEvent(new Event('visibilitychange'));
	await settle();
	assert.equal(fetcher.mock.callCount(), 3, 'one watcher still owns visibility refresh');
	assert.equal(worker.online, false);
	assert.equal(worker.gpu, null);

	let reply: (response: Response) => void = () => {};
	answer = new Promise<Response>((resolve) => {
		reply = resolve;
	});
	context.mock.timers.tick(15000);
	await settle();
	stopSecond();
	assert.equal(signals.at(-1)?.aborted, true);
	assert.equal(worker.online, null);
	assert.equal(worker.gpu, null);
	reply(Response.json({ gpu: { device: 'late-gpu' } }));
	await settle();
	assert.equal(worker.online, null);
	context.mock.timers.tick(15000);
	events.dispatchEvent(new Event('visibilitychange'));
	await settle();
	assert.equal(fetcher.mock.callCount(), 4, 'last stop removes the interval and listener');

	answer = Response.json({ gpu: null });
	watch();
	await settle();
	assert.equal(fetcher.mock.callCount(), 5, 'a new first watcher restarts polling');
	assert.equal(worker.online, true, 'a worker without GPU metrics is still connected');
	assert.equal(worker.gpu, null);
});

test('non-admin roles cannot read worker state and a role change stops polling', async (context) => {
	const { events, watch } = page(context);
	context.mock.timers.enable({ apis: ['setInterval'] });
	account.current = { email: 'user@example.com', role: 'user' };
	const signals: AbortSignal[] = [];
	const fetcher = context.mock.method(
		globalThis,
		'fetch',
		async (_input: RequestInfo | URL, init?: RequestInit) => {
			if (init?.signal instanceof AbortSignal) signals.push(init.signal);
			return Response.json({ gpu: { device: 'test-gpu' } });
		}
	);
	watch();
	await settle();
	context.mock.timers.tick(15000);
	events.dispatchEvent(new Event('visibilitychange'));
	await settle();
	assert.equal(fetcher.mock.callCount(), 0);
	assert.equal(worker.online, null);

	account.current = { email: 'admin@example.com', role: 'admin' };
	await settle();
	assert.equal(fetcher.mock.callCount(), 1);
	assert.equal(worker.online, true);
	account.current = { email: 'viewer@example.com', role: 'viewer' };
	await settle();
	assert.equal(signals.at(-1)?.aborted, true);
	assert.equal(worker.online, null);
	assert.equal(worker.gpu, null);
	context.mock.timers.tick(15000);
	events.dispatchEvent(new Event('visibilitychange'));
	await settle();
	assert.equal(fetcher.mock.callCount(), 1);
});
