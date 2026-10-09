import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { applyLandingMode, readLandingMode } from './landing-mode.ts';

const themeColor = {
	content: '#070b14',
	setAttribute: (_: string, value: string) => void (themeColor.content = value)
};

function page(storage: Pick<Storage, 'getItem' | 'setItem'>) {
	const dataset: Record<string, string> = {};
	Object.assign(globalThis, {
		document: { documentElement: { dataset }, querySelector: () => themeColor },
		localStorage: storage
	});
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

test('the browser chrome color follows the theme', () => {
	page(memoryStorage());
	applyLandingMode('light');
	assert.equal(themeColor.content, '#f3f5f8');
	applyLandingMode('dark');
	assert.equal(themeColor.content, '#070b14');
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

test('app.html theme-color meta is positioned before the script and sets light mode to #f3f5f8', () => {
	const html = readFileSync(new URL('../app.html', import.meta.url), 'utf8');
	const themeMetaMatch = html.match(/<meta name="theme-color"[^>]*>/g);
	assert.equal(themeMetaMatch?.length, 1, 'app.html must contain exactly one theme-color meta');
	const themeMetaPos = html.indexOf('<meta name="theme-color"');
	const scriptPos = html.indexOf('<script>');
	assert.ok(themeMetaPos < scriptPos, 'theme-color meta must appear before the script');
	const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? '';
	assert.ok(script.includes("'#f3f5f8'"), 'script must set theme color to #f3f5f8 for light mode');
});

test('the pre-paint script lightens the browser bar on landing pages only', async () => {
	const html = readFileSync(new URL('../app.html', import.meta.url), 'utf8');
	const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] ?? '';
	const { runInNewContext } = await import('node:vm');
	const colorAfterLoad = (pathname: string) => {
		const meta = {
			content: '#070b14',
			setAttribute: (_: string, value: string) => void (meta.content = value)
		};
		runInNewContext(script, {
			localStorage: { getItem: () => 'light' },
			location: { pathname },
			document: { documentElement: { dataset: {} }, querySelector: () => meta }
		});
		return meta.content;
	};
	for (const path of ['/', '/whitepaper', '/benchmark', '/illusions']) {
		assert.equal(colorAfterLoad(path), '#f3f5f8', path);
	}
	for (const path of ['/app', '/login', '/privacy']) {
		assert.equal(colorAfterLoad(path), '#070b14', path);
	}
});
