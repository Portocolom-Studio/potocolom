import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('the built landing /app contains the studio preview', async () => {
	const html = await readFile(new URL('../build/app.html', import.meta.url), 'utf8');
	const body = html.slice(html.indexOf('<body'), html.indexOf('</body>'));
	assert.match(body, /<div\s[^>]*\bdata-studio-preview(?:\s|[=>])/);
});
