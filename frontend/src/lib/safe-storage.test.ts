import assert from 'node:assert/strict';
import { test } from 'node:test';

import { readStored, removeStored, writeStored } from './safe-storage.ts';

function defineBlockedStorage(): void {
	Object.defineProperty(globalThis, 'localStorage', {
		configurable: true,
		get() {
			throw new Error('SecurityError');
		}
	});
}

function defineWorkingStorage(): void {
	const values = new Map<string, string>();
	Object.defineProperty(globalThis, 'localStorage', {
		configurable: true,
		value: {
			getItem: (key: string) => values.get(key) ?? null,
			setItem: (key: string, value: string) => void values.set(key, value),
			removeItem: (key: string) => void values.delete(key)
		}
	});
}

function dropStorage(): void {
	delete (globalThis as { localStorage?: unknown }).localStorage;
}

test('blocked storage reads as null and writes do not throw', () => {
	defineBlockedStorage();
	assert.equal(readStored('locale'), null);
	assert.doesNotThrow(() => writeStored('locale', 'es'));
	assert.doesNotThrow(() => removeStored('locale'));
	dropStorage();
});

test('working storage round-trips a write then a read, and remove clears it', () => {
	defineWorkingStorage();
	writeStored('locale', 'es');
	assert.equal(readStored('locale'), 'es');
	removeStored('locale');
	assert.equal(readStored('locale'), null);
	dropStorage();
});

test('the server has no localStorage and reads as null', () => {
	dropStorage();
	assert.equal(readStored('locale'), null);
	assert.doesNotThrow(() => writeStored('locale', 'es'));
	assert.doesNotThrow(() => removeStored('locale'));
});
