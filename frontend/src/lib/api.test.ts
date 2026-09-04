import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { csrfHeaders, readCsrfToken } from './api.ts';

const here = dirname(fileURLToPath(import.meta.url));
const apiSource = readFileSync(join(here, 'api.ts'), 'utf8');

test('csrf header is sent when the cookie is present', () => {
	const headers = csrfHeaders('potocolom_csrf=abc123; other=value');
	assert.equal(headers['x-csrf-token'], 'abc123');
});

test('csrf header is omitted when no cookie exists', () => {
	assert.deepEqual(csrfHeaders('other=value'), {});
});

test('host-prefixed csrf cookie is read', () => {
	assert.equal(readCsrfToken('__Host-potocolom_csrf=host-token'), 'host-token');
});

test('wiring: api helper sets x-csrf-token from cookies', () => {
	assert.match(apiSource, /x-csrf-token/);
});
