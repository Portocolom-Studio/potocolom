import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	isStripNavKey,
	nextFocusIndex,
	thumbnailLabel,
	type StripNavKey
} from './history-strip-nav.ts';

test('nextFocusIndex clamps ArrowLeft at the first thumbnail', () => {
	assert.equal(nextFocusIndex(0, 5, 'ArrowLeft'), 0);
	assert.equal(nextFocusIndex(3, 5, 'ArrowLeft'), 2);
});

test('nextFocusIndex clamps ArrowRight at the last thumbnail', () => {
	assert.equal(nextFocusIndex(4, 5, 'ArrowRight'), 4);
	assert.equal(nextFocusIndex(3, 5, 'ArrowRight'), 4);
});

test('nextFocusIndex jumps Home to the first and End to the last', () => {
	assert.equal(nextFocusIndex(3, 5, 'Home'), 0);
	assert.equal(nextFocusIndex(0, 5, 'Home'), 0);
	assert.equal(nextFocusIndex(1, 5, 'End'), 4);
	assert.equal(nextFocusIndex(4, 5, 'End'), 4);
});

test('nextFocusIndex stays put with one thumbnail or none', () => {
	assert.equal(nextFocusIndex(0, 1, 'ArrowLeft'), 0);
	assert.equal(nextFocusIndex(0, 1, 'ArrowRight'), 0);
	assert.equal(nextFocusIndex(2, 0, 'ArrowRight'), 2);
	assert.equal(nextFocusIndex(2, 0, 'End'), 2);
});

test('isStripNavKey accepts exactly the four navigation keys', () => {
	for (const key of ['ArrowLeft', 'ArrowRight', 'Home', 'End'] as StripNavKey[]) {
		assert.equal(isStripNavKey(key), true);
	}
	assert.equal(isStripNavKey('Tab'), false);
	assert.equal(isStripNavKey('Enter'), false);
	assert.equal(isStripNavKey(''), false);
});

test('thumbnailLabel keeps a real prompt verbatim', () => {
	assert.equal(thumbnailLabel('a castle on a hill', 'Untitled generation'), 'a castle on a hill');
});

test('thumbnailLabel falls back to untitled for null, undefined, empty and whitespace prompts', () => {
	assert.equal(thumbnailLabel(null, 'Untitled generation'), 'Untitled generation');
	assert.equal(thumbnailLabel(undefined, 'Untitled generation'), 'Untitled generation');
	assert.equal(thumbnailLabel('', 'Untitled generation'), 'Untitled generation');
	assert.equal(thumbnailLabel('   ', 'Untitled generation'), 'Untitled generation');
	assert.equal(thumbnailLabel('\n\t ', 'Untitled generation'), 'Untitled generation');
});
