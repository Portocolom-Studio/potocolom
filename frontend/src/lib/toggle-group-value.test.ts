import assert from 'node:assert/strict';
import { test } from 'node:test';

import { acceptsToggleValue } from './toggle-group-value.ts';

test('a single group refuses the empty value bits-ui sends on a second click', () => {
	assert.equal(acceptsToggleValue('single', ''), false);
	assert.equal(acceptsToggleValue('single', '768'), true);
});

test('a multiple group may be emptied', () => {
	assert.equal(acceptsToggleValue('multiple', []), true);
	assert.equal(acceptsToggleValue('multiple', ''), true);
});
