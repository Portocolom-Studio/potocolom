import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

type I18n = typeof import('./i18n.svelte.ts');

const here = dirname(fileURLToPath(import.meta.url));
const globals = globalThis as unknown as Record<string, unknown>;
const saved = new Map<string, unknown>();

function stub(name: string, value: unknown): void {
	if (!saved.has(name)) saved.set(name, globals[name]);
	globals[name] = value;
}

function stubDocument(): { documentElement: { lang: string } } {
	const document = { documentElement: { lang: '' } };
	stub('document', document);
	return document;
}

function storage(values: Map<string, string>) {
	return {
		getItem: (key: string) => values.get(key) ?? null,
		setItem: (key: string, value: string) => void values.set(key, value),
		removeItem: (key: string) => void values.delete(key)
	};
}

function blockedStorage() {
	const blocked = (): never => {
		throw new Error('SecurityError');
	};
	return { getItem: blocked, setItem: blocked, removeItem: blocked };
}

afterEach(() => {
	for (const [name, value] of saved) {
		globals[name] = value;
	}
	saved.clear();
});

let freshLoads = 0;
// The locale lives in module state, so every test loads its own copy and none
// of them depends on the order the file runs in.
async function freshI18n(): Promise<I18n> {
	freshLoads += 1;
	const url = new URL(`./i18n.svelte.ts?fresh=${freshLoads}`, import.meta.url).href;
	return (await import(url)) as I18n;
}

test('setLocale stores the locale and sets the document language', async () => {
	const i18n = await freshI18n();
	const values = new Map<string, string>();
	const document = stubDocument();
	stub('localStorage', storage(values));

	i18n.setLocale('es');

	assert.equal(values.get('locale'), 'es');
	assert.equal(i18n.getLocale(), 'es');
	assert.equal(document.documentElement.lang, 'es');
	assert.equal(i18n.t('nav.launch'), 'Abrir la app');
});

test('initializeLocale restores the stored locale on a fresh load', async () => {
	const document = stubDocument();
	stub('localStorage', storage(new Map([['locale', 'es']])));
	const i18n = await freshI18n();

	i18n.initializeLocale();

	assert.equal(i18n.getLocale(), 'es');
	assert.equal(document.documentElement.lang, 'es');
	assert.equal(i18n.t('nav.launch'), 'Abrir la app');
});

test('setLocale still switches the dictionary and the language when storage is blocked', async () => {
	const i18n = await freshI18n();
	const document = stubDocument();
	stub('localStorage', blockedStorage());

	assert.equal(i18n.getLocale(), 'en');
	assert.equal(i18n.t('nav.launch'), 'Launch the app');

	i18n.setLocale('es');

	assert.equal(i18n.getLocale(), 'es');
	assert.equal(document.documentElement.lang, 'es');
	assert.equal(i18n.t('nav.launch'), 'Abrir la app');
});

test('formatDateTime formats an ISO date per locale and passes unusable values through', async () => {
	const i18n = await freshI18n();
	stubDocument();
	stub('localStorage', storage(new Map()));

	assert.equal(i18n.formatDateTime(null), '-');
	assert.equal(i18n.formatDateTime('the other day'), 'the other day');

	const iso = '2024-05-06T15:30:00.000Z';
	i18n.setLocale('en');
	const english = i18n.formatDateTime(iso);
	i18n.setLocale('es');
	const spanish = i18n.formatDateTime(iso);
	assert.notEqual(english, spanish, 'the same instant reads differently per locale');
});

test('wiring: the layout restores the locale once it mounts', () => {
	const layout = readFileSync(join(here, '../routes/+layout.svelte'), 'utf8');
	assert.match(layout, /initializeLocale/);
});
