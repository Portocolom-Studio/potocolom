import assert from 'node:assert/strict';
import test from 'node:test';
import type { Generation } from './studio.svelte.ts';
import {
	GRID_RESUME_LIMIT,
	MAX_GRID_VISIT_ROWS,
	imagesGridSnapshotKey,
	makeGridVisitSnapshot,
	mayPrependLiveGridRows,
	restoredGridAnchor,
	rowsForSnapshot,
	selectionInGridWindow,
	snapshotMatchesCriteria
} from './images-grid-resume.ts';

function rows(count: number): Generation[] {
	return Array.from({ length: count }, (_, index) => ({
		id: `job-${index}`,
		model_id: 'model',
		params: {},
		state: 'succeeded',
		created_at: new Date(2026, 0, 1, 0, 0, -index).toISOString(),
		dispatched_at: null,
		finished_at: null,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		gpu_ms: null,
		progress: null,
		failure_reason: null,
		source_asset_id: null,
		has_derivatives: false,
		starred_at: null,
		expired: false,
		expired_favorite: false,
		assets: [
			{
				id: `asset-${index}`,
				url: `/api/v1/assets/asset-${index}`,
				thumbnail_url: null,
				download_url: `/api/v1/assets/asset-${index}`,
				width: 512,
				height: 512
			}
		]
	}));
}

test('one owner epoch has one grid snapshot key', () => {
	assert.equal(imagesGridSnapshotKey(42), 'images:grid:42');
	assert.notEqual(imagesGridSnapshotKey(42), imagesGridSnapshotKey(43));
});

test('live rows prepend only when the current window starts at newest', () => {
	assert.equal(mayPrependLiveGridRows('newest'), true);
	assert.equal(mayPrependLiveGridRows('cursor'), false);
});

test('a snapshot keeps only the newest bounded server rows and repairs its cursor', () => {
	const snapshot = makeGridVisitSnapshot({
		criteriaKey: 'model=a',
		rows: rows(MAX_GRID_VISIT_ROWS + 25),
		beforeCursor: null,
		window: 'newest',
		hasMore: false
	});
	assert.equal(snapshot.rows.length, MAX_GRID_VISIT_ROWS);
	assert.equal(snapshot.cursor, `job-${MAX_GRID_VISIT_ROWS - 1}`);
	assert.equal(snapshot.hasMore, true);
	assert.equal(snapshot.window, 'newest');
	assert.equal(snapshot.provenance, 'server-success');
	assert.equal(GRID_RESUME_LIMIT, 200);
});

test('a successful empty tail is valid snapshot data for its exact criteria', () => {
	const snapshot = makeGridVisitSnapshot({
		criteriaKey: 'starred=true',
		rows: [],
		beforeCursor: 'valid-before',
		window: 'cursor',
		hasMore: false
	});
	assert.equal(snapshotMatchesCriteria(snapshot, 'starred=true'), true);
	assert.equal(snapshot.window, 'cursor');
	assert.deepEqual(rowsForSnapshot(snapshot, 'starred=true'), []);
	assert.equal(snapshotMatchesCriteria(snapshot, 'starred=false'), false);
});

test('resume anchors and selections require membership in the authoritative window', () => {
	const page = rows(3);
	const anchor = { id: 'job-1', beforeCursor: 'job-newer', offset: 28 };
	assert.deepEqual(restoredGridAnchor(page, anchor), anchor);
	assert.equal(restoredGridAnchor(page, { ...anchor, id: 'deleted' }), null);
	assert.equal(selectionInGridWindow(page, 'job-2'), 'job-2');
	assert.equal(selectionInGridWindow(page, 'outside-window'), null);
	assert.equal(selectionInGridWindow(page, null), null);
});
