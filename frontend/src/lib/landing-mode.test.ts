import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { applyLandingMode, readLandingMode } from './landing-mode.ts';

function page(storage: Pick<Storage, 'getItem' | 'setItem'>) {
	const dataset: Record<string, string> = {};
	Object.assign(globalThis, { document: { documentElement: { dataset } }, localStorage: storage });
	return dataset;
}

function memoryStorage() {
	const items = new Map<string, string>();
	return {
		getItem: (key: string) => items.get(key) ?? null,
		setItem: (key: string, value: string) => void items.set(key, value)
	};
}

test('the theme defaults to dark and remembers a switch to light', () => {
	const dataset = page(memoryStorage());
	assert.equal(readLandingMode(), 'dark');
	applyLandingMode('light');
	assert.equal(dataset.landingMode, 'light');
	assert.equal(readLandingMode(), 'light');
});

test('applying the stored theme on load does not write it back', () => {
	const storage = memoryStorage();
	let writes = 0;
	const dataset = page({ getItem: storage.getItem, setItem: () => void writes++ });
	applyLandingMode('light', false);
	assert.equal(dataset.landingMode, 'light');
	assert.equal(writes, 0);
});

test('blocked storage still switches the theme for the visit', () => {
	const dataset = page({
		getItem: () => {
			throw new Error('blocked');
		},
		setItem: () => {
			throw new Error('blocked');
		}
	});
	assert.equal(readLandingMode(), 'dark');
	applyLandingMode('light');
	assert.equal(dataset.landingMode, 'light');
});

test('the pre-paint theme script in app.html is allowed by the CSP hash', () => {
	const html = readFileSync(new URL('../app.html', import.meta.url), 'utf8');
	const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? '';
	assert.ok(script.includes('landing-mode'));
	const hash = `sha256-${createHash('sha256').update(script).digest('base64')}`;
	const config = readFileSync(new URL('../../vite.config.ts', import.meta.url), 'utf8');
	assert.ok(config.includes(`'${hash}'`), `vite.config.ts script-src lacks ${hash}`);
});
