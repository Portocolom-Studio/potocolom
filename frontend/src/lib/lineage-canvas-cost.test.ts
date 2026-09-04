import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { measureCanvasCosts } from './lineage-canvas-cost.ts';

const here = dirname(fileURLToPath(import.meta.url));

test('a few thousand nodes can be measured without changing production', () => {
	const report = measureCanvasCosts({ nodeCount: 3000, treeCount: 50, frames: 80 });
	assert.ok(report.nodeCount >= 2500, `expected thousands of nodes, got ${report.nodeCount}`);
	assert.ok(report.edgeCount > 0);
	assert.ok(Number.isFinite(report.edgeIfPerFrameMs));
	assert.ok(Number.isFinite(report.edgeEachPerFrameMs));
	assert.ok(Number.isFinite(report.viewportPerFrameMs));
	assert.ok(Number.isFinite(report.edgeIfAllVisiblePerFrameMs));
	const canvas = readFileSync(join(here, 'components/lineage-canvas.svelte'), 'utf8');
	assert.match(canvas, /\{#if rectsIntersect/);
	assert.match(canvas, /function lineageViewport\(/);
});
