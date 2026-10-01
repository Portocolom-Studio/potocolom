import assert from 'node:assert/strict';
import { test } from 'node:test';

import { palmRejected, strokePointWidth } from './drawing-document.ts';

test('strokePointWidth scales between a quarter and full size with pressure', () => {
	assert.equal(strokePointWidth(20, undefined), 20, 'no pressure paints at full size');
	assert.equal(strokePointWidth(20, 0), 5, 'zero pressure paints at a quarter size');
	assert.equal(strokePointWidth(20, 1), 20, 'full pressure paints at full size');
	assert.equal(strokePointWidth(20, 0.5), 12.5, 'half pressure paints at the midpoint');
});

test('palmRejected ignores a touch pointer only while a pen stroke is active', () => {
	assert.equal(palmRejected('pen', 'touch'), true);
	assert.equal(palmRejected('pen', 'pen'), false, 'a second pen pointer is not a palm');
	assert.equal(palmRejected('pen', 'mouse'), false);
	assert.equal(palmRejected('touch', 'touch'), false, 'no pen stroke means nothing to protect');
	assert.equal(
		palmRejected(null, 'touch'),
		false,
		'no stroke in progress means nothing to protect'
	);
});

// Validation is exercised through the exported DrawingDocument class: it has
// no instance state that validateDrawingFile needs, so restoring into a
// throwaway document is the simplest way to reach the file-format rules
// without re-implementing them here.
async function restoreError(file: unknown): Promise<string> {
	const { DrawingDocument } = await import('./drawing-document.ts');
	class FakeContext {
		save() {}
		restore() {}
		fillRect() {}
		beginPath() {}
		arc() {}
		fill() {}
		moveTo() {}
		lineTo() {}
		stroke() {}
		rect() {}
		ellipse() {}
		closePath() {}
		getImageData() {
			return { data: new Uint8ClampedArray(4 * 512 * 512).fill(255) };
		}
	}
	const canvas = {
		width: 512,
		height: 512,
		getContext: () => new FakeContext()
	} as unknown as HTMLCanvasElement;
	const document = new DrawingDocument(canvas);
	try {
		document.restore(file);
		return '';
	} catch (error) {
		return error instanceof Error ? error.message : String(error);
	}
}

function baseFile(version: number, points: unknown[]) {
	return {
		version,
		width: 512,
		height: 512,
		operations: [
			{
				kind: 'stroke',
				id: 'operation-1',
				mode: 'draw',
				color: '#111827',
				size: 6,
				points
			}
		],
		cursor: 1
	};
}

test('a version 4 stroke point accepts pressure in range', async () => {
	const error = await restoreError(baseFile(4, [{ x: 1, y: 1, pressure: 0.4 }]));
	assert.equal(error, '');
});

test('a version 3 stroke point rejects pressure', async () => {
	const error = await restoreError(baseFile(3, [{ x: 1, y: 1, pressure: 0.4 }]));
	assert.equal(error, 'invalid drawing point');
});

test('a version 4 stroke point rejects out-of-range pressure', async () => {
	assert.equal(
		await restoreError(baseFile(4, [{ x: 1, y: 1, pressure: 1.1 }])),
		'invalid drawing point'
	);
	assert.equal(
		await restoreError(baseFile(4, [{ x: 1, y: 1, pressure: -0.1 }])),
		'invalid drawing point'
	);
});
