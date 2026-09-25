import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// studio-benchmark-sessions.ts imports the SvelteKit virtual module
// $env/static/public, which only resolves inside a Vite build. A plain
// `node --test` run cannot import the file directly, so a stand-in copy with
// that one import swapped for a literal is written to the worktree's scratch
// directory and imported from there instead. Nothing under test changes.
const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, 'studio-benchmark-sessions.ts'), 'utf8');
const envImport = "import { PUBLIC_SITE_MODE } from '$env/static/public';";
assert.ok(source.includes(envImport), 'expected $env import to stub out for testing');
const stubbed = source.replace(envImport, "const PUBLIC_SITE_MODE = 'studio';");
const stackDir = join(here, '../../../.stack');
mkdirSync(stackDir, { recursive: true });
const stubPath = join(stackDir, 'studio-benchmark-sessions.no-env.ts');
writeFileSync(stubPath, stubbed);
const { mayReadSessions } = await import(pathToFileURL(stubPath).href);

test('AUTH_MODE=none asks: empty auth_methods means the implicit user is admin', () => {
	assert.equal(mayReadSessions({ authMethods: [], hasCsrf: false, role: null }), true);
});

test('accounts mode with no session asks nothing', () => {
	assert.equal(mayReadSessions({ authMethods: ['password'], hasCsrf: false, role: null }), false);
});

test('accounts mode signed in as admin asks', () => {
	assert.equal(mayReadSessions({ authMethods: ['password'], hasCsrf: true, role: 'admin' }), true);
});

test('accounts mode signed in as a non-admin does not ask', () => {
	assert.equal(mayReadSessions({ authMethods: ['password'], hasCsrf: true, role: 'user' }), false);
	assert.equal(
		mayReadSessions({ authMethods: ['password'], hasCsrf: true, role: 'viewer' }),
		false
	);
});

test('a failed config probe does not ask', () => {
	assert.equal(mayReadSessions({ authMethods: null, hasCsrf: false, role: null }), false);
});
