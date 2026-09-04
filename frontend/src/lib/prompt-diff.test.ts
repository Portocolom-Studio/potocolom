import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { collapsePromptDiff, paramDeltas, promptWordDiff } from './prompt-diff.ts';

const here = dirname(fileURLToPath(import.meta.url));

test('prompt word diff names added and removed terms', () => {
	const tokens = promptWordDiff('a red house at dusk', 'a blue house at night');
	assert.deepEqual(tokens, [
		{ kind: 'same', text: 'a' },
		{ kind: 'removed', text: 'red' },
		{ kind: 'added', text: 'blue' },
		{ kind: 'same', text: 'house at' },
		{ kind: 'removed', text: 'dusk' },
		{ kind: 'added', text: 'night' }
	]);
});

test('a long unchanged middle is truncated around the hunks', () => {
	const parent = 'start one two three four five six seven end';
	const child = 'start one two three four five six seven last';
	const full = promptWordDiff(parent, child);
	const collapsed = collapsePromptDiff(full, 1);
	assert.equal(collapsed.truncated, true);
	assert.equal(
		collapsed.tokens.some((token) => token.kind === 'same' && token.text.includes('two')),
		false
	);
	assert.equal(
		collapsed.tokens.some((token) => token.kind === 'removed' && token.text === 'end'),
		true
	);
});

test('seed changes collapse to new seed and prompt is compared separately', () => {
	assert.deepEqual(
		paramDeltas(
			{ prompt: 'old', seed: 1, steps: 4, extra: 'gone' },
			{ prompt: 'new', seed: 9, steps: 8, strength: 0.6 }
		),
		[
			{ kind: 'removed', key: 'extra', value: 'gone' },
			{ kind: 'changed', key: 'steps', from: '4', to: '8' },
			{ kind: 'added', key: 'strength', value: '0.6' },
			{ kind: 'new-seed' }
		]
	);
});

test('the canvas wires double-click branch and card-LOD deltas', () => {
	const source = readFileSync(join(here, 'components/lineage-canvas.svelte'), 'utf8');
	assert.match(source, /ondblclick/);
	assert.match(source, /promptWordDiff/);
	assert.match(source, /paramDeltas/);
	assert.match(source, /branchFromNode/);
});
