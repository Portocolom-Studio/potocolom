import assert from 'node:assert/strict';
import { test } from 'node:test';

import en from './i18n/en.json';
import es from './i18n/es.json';
import {
	defaultGpuOption,
	GPU_CARD_DONE_KEY,
	gpuCardOpensFleet,
	markGpuCardDone,
	shouldShowGpuCard
} from './gpu-setup-card.ts';

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
