import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));

test('locale is stored and restored across loads', () => {
	const i18n = readFileSync(join(here, 'i18n.svelte.ts'), 'utf8');
	const layout = readFileSync(join(here, '../routes/+layout.svelte'), 'utf8');
	assert.match(i18n, /localStorage\.setItem\('locale'/);
	assert.match(i18n, /localStorage\.getItem\('locale'/);
	assert.match(layout, /initializeLocale/);
});
