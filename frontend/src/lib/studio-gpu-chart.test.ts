import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

type I18n = typeof import('./i18n.svelte.ts');

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

afterEach(() => {
	for (const [name, value] of saved) {
		globals[name] = value;
	}
	saved.clear();
});

test('formatTimeTick uses app locale, not browser locale', async () => {
	stubDocument();
	stub('localStorage', storage(new Map()));

	const i18n = await import('./i18n.svelte.ts');
	const chart = await import('./studio-gpu-chart.ts');

	const timestamp = new Date('2024-05-06T15:30:45.000Z').getTime();

	i18n.setLocale('en');
	const english = chart.formatTimeTick(timestamp, '5m');

	i18n.setLocale('es');
	const spanish = chart.formatTimeTick(timestamp, '5m');

	assert.notEqual(
		english,
		spanish,
		`formatTimeTick should differ by locale: en="${english}" es="${spanish}"`
	);
});

test('formatRangeCaption uses app locale, not browser locale', async () => {
	stubDocument();
	stub('localStorage', storage(new Map()));

	const i18n = await import('./i18n.svelte.ts');
	const chart = await import('./studio-gpu-chart.ts');

	const start = new Date('2024-05-06T15:30:00.000Z').getTime();
	const end = new Date('2024-05-06T16:30:00.000Z').getTime();

	i18n.setLocale('en');
	const english = chart.formatRangeCaption(start, end, '5m');

	i18n.setLocale('es');
	const spanish = chart.formatRangeCaption(start, end, '5m');

	assert.notEqual(
		english,
		spanish,
		`formatRangeCaption should differ by locale: en="${english}" es="${spanish}"`
	);
});
