import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import {
	lineageSearchMatchPositions,
	lineageSearchMatchRootIds,
	lineageSearchTone,
	nextLineageMatchIndex
} from './lineage-search.ts';

const here = dirname(fileURLToPath(import.meta.url));

test('search overlay dims non-matches and rings matches', () => {
	const matches = new Set(['hit']);
	assert.equal(lineageSearchTone('hit', matches), 'match');
	assert.equal(lineageSearchTone('miss', matches), 'dim');
	assert.equal(lineageSearchTone('hit', null), 'plain');
});

test('next match wraps around the match list', () => {
	assert.equal(nextLineageMatchIndex(-1, 3), 0);
	assert.equal(nextLineageMatchIndex(0, 3), 1);
	assert.equal(nextLineageMatchIndex(2, 3), 0);
});

test('match positions walk packed trees without changing layout', () => {
	const trees = [
		{
			rootId: 'root-a',
			x: 10,
			y: 20,
			layout: {
				nodes: [
					{ x: 0, y: 0, data: { entry: { job_id: 'hit' } } },
					{ x: 5, y: 8, data: { entry: { job_id: 'miss' } } }
				]
			}
		}
	];
	const matches = new Set(['hit']);
	assert.deepEqual(lineageSearchMatchPositions(trees, matches), [
		{ jobId: 'hit', x: 10, y: 20 }
	]);
	assert.deepEqual([...lineageSearchMatchRootIds(trees, matches)], ['root-a']);
	assert.equal(lineageSearchMatchRootIds(trees, null).size, 0);
});

test('the canvas overlay searches with ids mode and does not relayout', () => {
	const source = readFileSync(join(here, 'components/lineage-canvas.svelte'), 'utf8');
	assert.match(source, /fields:\s*'ids'/);
	assert.match(source, /search\.set\('q'/);
	assert.match(source, /lineageSearchTone/);
	assert.match(source, /nextLineageMatchIndex/);
	assert.doesNotMatch(source, /query\.set\('q'/);
});
