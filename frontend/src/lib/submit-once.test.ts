import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { runExclusive } from './submit-once.ts';

const here = dirname(fileURLToPath(import.meta.url));

test('a second submit while the first is in flight is ignored', async () => {
	const lock = { busy: false };
	let started = 0;
	let finishFirst: () => void = () => {};
	const firstWork = new Promise<void>((resolve) => {
		finishFirst = resolve;
	});
	const first = runExclusive(lock, async () => {
		started += 1;
		await firstWork;
	});
	const second = runExclusive(lock, async () => {
		started += 1;
	});
	assert.equal(started, 1);
	assert.equal(await second, false);
	finishFirst();
	assert.equal(await first, true);
	assert.equal(started, 1);
	assert.equal(lock.busy, false);
});

test('generate-panel wires the exclusive submit', () => {
	const source = readFileSync(join(here, 'components/generate-panel.svelte'), 'utf8');
	assert.match(source, /runExclusive/);
	assert.match(source, /disabled=\{[\s\S]*submitLock\.busy/);
	assert.match(source, /onclick=\{upscaleShown\}/);
});

test('the header does not keep a search box that ignores input', () => {
	const header = readFileSync(join(here, 'components/site-header.svelte'), 'utf8');
	assert.doesNotMatch(header, /SearchForm/);
	assert.doesNotMatch(header, /search-form/);
});
