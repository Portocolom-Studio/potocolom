import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	clearGpuSamples,
	gpuSamples,
	lastGpuHardware,
	startGpuSampler,
	stopGpuSampler,
	subscribeGpuSamples
} from './studio-gpu-sampler.ts';

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((resolvePromise) => {
		resolve = resolvePromise;
	});
	return { promise, resolve };
}

test('a late old request cannot detach a pending new-owner request', async (context) => {
	const originalFetch = globalThis.fetch;
	const requests: Array<{
		signal: AbortSignal | null;
		response: ReturnType<typeof deferred<Response>>;
	}> = [];
	let notifications = 0;
	context.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
	const unsubscribe = subscribeGpuSamples(() => notifications++);
	try {
		globalThis.fetch = ((_: RequestInfo | URL, init?: RequestInit) => {
			const response = deferred<Response>();
			requests.push({ signal: init?.signal as AbortSignal | null, response });
			return response.promise;
		}) as typeof fetch;

		startGpuSampler(() => []);
		assert.equal(requests.length, 1);
		const oldRequest = requests[0];

		stopGpuSampler();
		clearGpuSamples();
		assert.equal(oldRequest.signal?.aborted, true);
		context.mock.timers.tick(2000);
		assert.equal(requests.length, 1, 'stop clears the old polling interval');

		startGpuSampler(() => []);
		assert.equal(requests.length, 2, 'the new owner starts a fresh request immediately');
		const newRequest = requests[1];
		oldRequest.response.resolve(
			new Response(
				JSON.stringify({ gpu: { device: 'owner-a-gpu', available: true, util_pct: 12 } })
			)
		);
		for (let index = 0; index < 8; index += 1) await Promise.resolve();
		context.mock.timers.tick(2000);
		assert.equal(
			requests.length,
			2,
			'the still-pending new request blocks an overlapping interval tick'
		);

		newRequest.response.resolve(
			new Response(
				JSON.stringify({ gpu: { device: 'owner-b-gpu', available: true, util_pct: 61 } })
			)
		);
		for (let index = 0; index < 8; index += 1) await Promise.resolve();

		assert.equal(
			lastGpuHardware()?.device,
			'owner-b-gpu',
			'the new reply publishes after the old reply'
		);
		assert.equal(gpuSamples().length, 1);
		assert.equal(notifications, 2, 'only clear and the new sample notify');
	} finally {
		stopGpuSampler();
		clearGpuSamples();
		unsubscribe();
		globalThis.fetch = originalFetch;
	}
});

test('an aborted timed-out request releases the sampler for the next interval', async (context) => {
	const originalFetch = globalThis.fetch;
	const signals: AbortSignal[] = [];
	let requestCount = 0;
	context.mock.timers.enable({ apis: ['setInterval', 'setTimeout'] });
	try {
		globalThis.fetch = ((_input: RequestInfo | URL, init?: RequestInit) => {
			requestCount += 1;
			const signal = init?.signal as AbortSignal;
			signals.push(signal);
			return new Promise<Response>((_resolve, reject) => {
				signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {
					once: true
				});
			}) as Promise<Response>;
		}) as typeof fetch;

		startGpuSampler(() => []);
		assert.equal(requestCount, 1);
		context.mock.timers.tick(5000);
		await Promise.resolve();
		await Promise.resolve();
		assert.equal(signals[0].aborted, true, 'the finite request deadline aborts the fetch');

		context.mock.timers.tick(2000);
		assert.equal(requestCount, 2, 'a later interval starts a new request after timeout cleanup');
		assert.equal(signals[1].aborted, false);
	} finally {
		stopGpuSampler();
		clearGpuSamples();
		globalThis.fetch = originalFetch;
	}
});
