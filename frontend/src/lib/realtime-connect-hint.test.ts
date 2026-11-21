import assert from 'node:assert/strict';
import { test } from 'node:test';

import { connectNeedsPrompt } from './realtime-connect-hint.ts';

test('an empty prompt is why Connect is disabled when the button is shown', () => {
	assert.equal(connectNeedsPrompt({ showConnect: true, canConnect: false, prompt: '  ' }), true);
	assert.equal(connectNeedsPrompt({ showConnect: true, canConnect: false, prompt: '' }), true);
});

test('a prompt, a live session, or a hidden Connect control does not ask for one', () => {
	assert.equal(
		connectNeedsPrompt({ showConnect: true, canConnect: false, prompt: 'red circle' }),
		false
	);
	assert.equal(connectNeedsPrompt({ showConnect: true, canConnect: true, prompt: '' }), false);
	assert.equal(connectNeedsPrompt({ showConnect: false, canConnect: false, prompt: '' }), false);
});
