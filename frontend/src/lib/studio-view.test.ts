import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	readStudioView,
	studioViewSearch,
	type MetricsTab,
	type ShellView
} from './studio-view.ts';

const at = (search: string) => new URL(`https://studio.test/app${search}`);

test('a bare /app is Generate on the usage tab', () => {
	assert.deepEqual(readStudioView(at('')), { view: 'generate', tab: 'usage' });
});

test('an unknown view falls back to Generate and an unknown tab to usage', () => {
	assert.deepEqual(readStudioView(at('?view=admin&tab=secrets')), {
		view: 'generate',
		tab: 'usage'
	});
});

test('the URL names the view and the benchmarks tab', () => {
	assert.deepEqual(readStudioView(at('?view=metrics&tab=benchmarks')), {
		view: 'metrics',
		tab: 'benchmarks'
	});
	assert.deepEqual(readStudioView(at('?view=models')), { view: 'models', tab: 'usage' });
});

test('Generate and the usage tab leave the URL bare', () => {
	assert.equal(studioViewSearch('?view=images', 'generate', 'usage'), '');
	assert.equal(studioViewSearch('', 'metrics', 'usage'), '?view=metrics');
	assert.equal(studioViewSearch('', 'metrics', 'benchmarks'), '?view=metrics&tab=benchmarks');
});

test('a tab is dropped on views that are not metrics', () => {
	assert.equal(
		studioViewSearch('?view=metrics&tab=benchmarks', 'upscale', 'benchmarks'),
		'?view=upscale'
	);
});

test('other query parameters are kept', () => {
	assert.equal(
		studioViewSearch('?ref=mail&view=models', 'images', 'usage'),
		'?ref=mail&view=images'
	);
	assert.equal(studioViewSearch('?ref=mail&view=models', 'generate', 'usage'), '?ref=mail');
});

test('the view already open builds the same search, so no history entry is pushed', () => {
	for (const search of ['', '?view=models&ref=mail', '?ref=mail&view=metrics&tab=benchmarks']) {
		const { view, tab } = readStudioView(at(search));
		assert.equal(studioViewSearch(search, view, tab), search);
	}
});

test('every view and tab round trips through the URL', () => {
	const views: ShellView[] = [
		'generate',
		'image_to_image',
		'upscale',
		'edit_image',
		'image_to_text',
		'realtime_canvas',
		'images',
		'models',
		'metrics'
	];
	const tabs: MetricsTab[] = ['usage', 'benchmarks'];
	for (const view of views) {
		for (const tab of tabs) {
			const read = readStudioView(at(studioViewSearch('', view, tab)));
			assert.equal(read.view, view);
			if (view === 'metrics') assert.equal(read.tab, tab);
		}
	}
});
