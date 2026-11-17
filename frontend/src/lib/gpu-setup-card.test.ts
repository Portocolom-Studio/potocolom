import assert from 'node:assert/strict';
import { test } from 'node:test';

import en from './i18n/en.json';
import es from './i18n/es.json';
import {
	defaultGpuOption,
	failureKey,
	firstModels,
	GPU_CARD_DONE_KEY,
	gpuCardOpensFleet,
	markGpuCardDone,
	readyCopy,
	reservedBytes,
	rowStatus,
	setupWorker,
	shouldShowGpuCard,
	skipsModelStep,
	type AdminWorker,
	type CardModel,
	type Download
} from './gpu-setup-card.ts';

const GIB = 1024 ** 3;

function memoryStorage(values = new Map<string, string>()) {
	return {
		values,
		getItem: (key: string) => values.get(key) ?? null,
		setItem: (key: string, value: string) => void values.set(key, value),
		removeItem: (key: string) => void values.delete(key)
	};
}

function refusingStorage(values = new Map<string, string>()) {
	return {
		...memoryStorage(values),
		setItem: () => {
			throw new Error('QuotaExceededError');
		}
	};
}

const blocked = () => {
	throw new Error('SecurityError');
};

function model(overrides: Partial<CardModel> & { id: string }): CardModel {
	return {
		name: overrides.id,
		capabilities: ['text_to_image'],
		installed: false,
		download_bytes: 6 * GIB,
		needs_token: false,
		download: null,
		...overrides
	};
}

function worker(overrides: Partial<AdminWorker> & { worker_id: string }): AdminWorker {
	return {
		device: 'rocm',
		manageable: true,
		hardware: {
			gpu_name: 'Test GPU',
			vram_total_bytes: 16 * GIB,
			disk_total_bytes: 500 * GIB,
			disk_free_bytes: 200 * GIB
		},
		disk_free_bytes: 200 * GIB,
		suggested: {},
		models: [],
		...overrides
	};
}

function download(overrides: Partial<Download>): Download {
	return {
		kind: 'install',
		state: 'downloading',
		bytes_done: 0,
		bytes_total: 0,
		error: null,
		op_id: '1',
		...overrides
	};
}

test('an administrator sees the card once, on a storage that can be read and written', () => {
	const storage = memoryStorage();
	assert.equal(
		shouldShowGpuCard(true, () => storage),
		true
	);
	assert.equal(storage.values.size, 0, 'the writability probe leaves nothing behind');
});

test('a viewer or user never sees the card, and storage is not touched', () => {
	const storage = memoryStorage();
	assert.equal(
		shouldShowGpuCard(false, () => storage),
		false
	);
	assert.equal(storage.values.size, 0);
});

test('a finished card stays shut', () => {
	const storage = memoryStorage(new Map([[GPU_CARD_DONE_KEY, '1']]));
	assert.equal(
		shouldShowGpuCard(true, () => storage),
		false
	);
});

test('blocked storage never shows the card', () => {
	assert.equal(shouldShowGpuCard(true, blocked), false);
});

test('storage that reads but refuses writes never shows the card', () => {
	assert.equal(
		shouldShowGpuCard(true, () => refusingStorage()),
		false
	);
});

test('marking the card done is what keeps it from showing again', () => {
	const storage = memoryStorage();
	markGpuCardDone(() => storage);
	assert.equal(storage.values.get(GPU_CARD_DONE_KEY), '1');
	assert.equal(
		shouldShowGpuCard(true, () => storage),
		false
	);
});

test('marking the card done on blocked storage does not throw', () => {
	assert.doesNotThrow(() => markGpuCardDone(blocked));
});

test('the default is "This computer" when one worker or more is connected', () => {
	assert.equal(defaultGpuOption(['m-a1']), 'this');
	assert.equal(defaultGpuOption(['m-a1', 'builtin-1']), 'this');
});

test('the default is "Other computers" only when the worker list loaded and is empty', () => {
	assert.equal(defaultGpuOption([]), 'other');
});

test('the default is "This computer" while the list loads and when it failed', () => {
	assert.equal(defaultGpuOption(null), 'this');
});

test('only "Other computers" opens the Add a GPU dialog', () => {
	assert.equal(gpuCardOpensFleet('other'), true);
	assert.equal(gpuCardOpensFleet('this'), false);
	assert.equal(gpuCardOpensFleet('dismissed'), false);
});

test('setupWorker picks the manageable worker with the most video memory', () => {
	const small = worker({
		worker_id: 'small',
		hardware: { ...worker({ worker_id: 'x' }).hardware!, vram_total_bytes: 8 * GIB }
	});
	const large = worker({
		worker_id: 'large',
		hardware: { ...worker({ worker_id: 'x' }).hardware!, vram_total_bytes: 24 * GIB }
	});
	assert.equal(setupWorker([small, large])?.worker_id, 'large');
	assert.equal(setupWorker([large, small])?.worker_id, 'large');
});

test('setupWorker skips workers that cannot be managed or have no hardware yet', () => {
	const unmanaged = worker({ worker_id: 'old', manageable: false });
	const booting = worker({ worker_id: 'new', hardware: null });
	assert.equal(setupWorker([unmanaged, booting]), null);
	assert.equal(setupWorker([]), null);
	assert.equal(setupWorker([unmanaged, booting, worker({ worker_id: 'ok' })])?.worker_id, 'ok');
});

test('firstModels lists the suggested uses in order, with size and installed state', () => {
	const w = worker({
		worker_id: 'w',
		suggested: { text_to_image: 'sdxl', realtime: 'fast', upscale: 'esrgan' },
		models: [
			model({ id: 'sdxl', name: 'SDXL', download_bytes: 7 * GIB, installed: true }),
			model({ id: 'fast', name: 'SDXL Fast', download_bytes: 6 * GIB }),
			model({ id: 'esrgan', name: 'Real-ESRGAN', download_bytes: null })
		]
	});
	const options = firstModels(w);
	assert.deepEqual(
		options.map((option) => [option.use, option.id, option.name, option.size, option.installed]),
		[
			['text_to_image', 'sdxl', 'SDXL', 7 * GIB, true],
			['realtime', 'fast', 'SDXL Fast', 6 * GIB, false],
			['upscale', 'esrgan', 'Real-ESRGAN', null, false]
		]
	);
});

test('a model suggested for two uses is offered once, under the first use', () => {
	const w = worker({
		worker_id: 'w',
		suggested: { text_to_image: 'both', upscale: 'both' },
		models: [model({ id: 'both', name: 'Both' })]
	});
	assert.deepEqual(
		firstModels(w).map((option) => option.use),
		['text_to_image']
	);
});

test('firstModels skips a use with no suggestion or a suggestion missing from the models', () => {
	const w = worker({
		worker_id: 'w',
		suggested: { text_to_image: null, realtime: 'ghost' },
		models: [model({ id: 'other' })]
	});
	assert.deepEqual(firstModels(w), []);
	assert.deepEqual(firstModels(worker({ worker_id: 'unmanaged', manageable: false })), []);
});

test('step 3 is passed over without a worker, without options, or when all are installed', () => {
	assert.equal(skipsModelStep(null), true);
	assert.equal(skipsModelStep(worker({ worker_id: 'w' })), true, 'no suggestions');
	const installed = worker({
		worker_id: 'w',
		suggested: { text_to_image: 'a' },
		models: [model({ id: 'a', installed: true })]
	});
	assert.equal(skipsModelStep(installed), true);
	const missing = worker({
		worker_id: 'w',
		suggested: { text_to_image: 'a', upscale: 'b' },
		models: [model({ id: 'a', installed: true }), model({ id: 'b' })]
	});
	assert.equal(skipsModelStep(missing), false);
});

test('reservedBytes counts what the running installs still have to fetch', () => {
	const idle = worker({ worker_id: 'w', models: [model({ id: 'a' })] });
	assert.equal(reservedBytes(idle), 0);
	const running = worker({
		worker_id: 'w',
		models: [
			model({
				id: 'a',
				download_bytes: 6 * GIB,
				download: download({ state: 'downloading', bytes_done: 2 * GIB, bytes_total: 6 * GIB })
			})
		]
	});
	assert.equal(reservedBytes(running), 4 * GIB);
	const unknown = worker({
		worker_id: 'w',
		models: [model({ id: 'a', download_bytes: null, download: download({ state: 'queued' }) })]
	});
	assert.equal(reservedBytes(unknown), null);
	const finished = worker({
		worker_id: 'w',
		models: [
			model({
				id: 'a',
				download_bytes: 6 * GIB,
				download: download({ state: 'failed', error: 'disk', bytes_done: 0, bytes_total: 6 * GIB })
			})
		]
	});
	assert.equal(reservedBytes(finished), 0, 'a stopped install reserves nothing');
});

test('step 4 says ready only when a text-to-image or realtime model is installed', () => {
	const running: Download = {
		kind: 'install',
		state: 'downloading',
		bytes_done: 1,
		bytes_total: 2,
		error: null,
		op_id: '1'
	};
	const none = [worker({ worker_id: 'w', models: [model({ id: 'a' })] })];
	assert.equal(
		readyCopy(none).title,
		'app.gpu_card.no_model_title',
		'nothing runs, nothing is ready'
	);
	const pending = [worker({ worker_id: 'w', models: [model({ id: 'a', download: running })] })];
	assert.equal(readyCopy(pending).title, 'app.gpu_card.downloading_title');
	const failed = [
		worker({
			worker_id: 'w',
			models: [model({ id: 'a', download: { ...running, state: 'failed', error: 'disk' } })]
		})
	];
	assert.equal(readyCopy(failed).title, 'app.gpu_card.no_model_title');
	assert.equal(readyCopy([]).title, 'app.gpu_card.no_gpu_title');
	assert.equal(
		readyCopy(null).title,
		'app.gpu_card.unread_title',
		'an unread list is not "no GPU"'
	);
	const upscaleOnly = [
		worker({
			worker_id: 'w',
			models: [model({ id: 'u', capabilities: ['upscale'], installed: true })]
		})
	];
	assert.equal(readyCopy(upscaleOnly).title, 'app.gpu_card.no_model_title');
	const generate = [worker({ worker_id: 'w', models: [model({ id: 'g', installed: true })] })];
	assert.equal(readyCopy(generate).title, 'app.gpu_card.ready_title');
	const live = [
		worker({
			worker_id: 'w',
			models: [model({ id: 'r', capabilities: ['realtime'], installed: true })]
		})
	];
	assert.equal(readyCopy(live).title, 'app.gpu_card.ready_title');
});

test('each download error has its own text, and anything else gets the generic one', () => {
	assert.equal(failureKey('disk'), 'app.gpu_card.failed_disk');
	assert.equal(failureKey('needs_token'), 'app.gpu_card.failed_token');
	assert.equal(failureKey('cancelled'), 'app.gpu_card.failed_cancelled');
	assert.equal(failureKey('in_use'), 'app.gpu_card.failed_other');
	assert.equal(failureKey(null), 'app.gpu_card.failed_other');
});

test('a row shows ready, progress, waiting or the failure text', () => {
	assert.deepEqual(rowStatus({ installed: true, size: null, download: null }), { kind: 'ready' });
	assert.deepEqual(rowStatus({ installed: false, size: 6 * GIB, download: null }), {
		kind: 'waiting'
	});
	assert.deepEqual(
		rowStatus({
			installed: false,
			size: 6 * GIB,
			download: download({ state: 'downloading', bytes_done: 4 * GIB, bytes_total: 6 * GIB })
		}),
		{ kind: 'progress', done: 4 * GIB, total: 6 * GIB }
	);
	assert.deepEqual(
		rowStatus({
			installed: false,
			size: null,
			download: download({ state: 'queued', bytes_done: 0, bytes_total: 0 })
		}),
		{ kind: 'waiting' },
		'no size yet means no bar'
	);
	assert.deepEqual(
		rowStatus({
			installed: false,
			size: 6 * GIB,
			download: download({ state: 'failed', error: 'disk' })
		}),
		{ kind: 'failed', key: 'app.gpu_card.failed_disk' }
	);
});

test('every card string exists in English and Spanish', () => {
	const keys = Object.keys(en).filter((key) => key.startsWith('app.gpu_card.'));
	assert.ok(keys.length > 0, 'the card has strings');
	for (const key of keys) {
		assert.ok(key in es, `${key} is missing from es.json`);
		assert.notEqual(en[key as keyof typeof en], '', `${key} is empty in en.json`);
	}
	for (const key of Object.keys(es).filter((item) => item.startsWith('app.gpu_card.'))) {
		assert.ok(key in en, `${key} is missing from en.json`);
	}
});
