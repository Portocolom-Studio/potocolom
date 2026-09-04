import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	createSubmitGuard,
	initialAuthView,
	readInviteTokenFromHash,
	shouldShowChallenge
} from './auth-flow.ts';

const here = dirname(fileURLToPath(import.meta.url));
const joinSource = readFileSync(join(here, '../routes/join/+page.svelte'), 'utf8');

test('login submit issues one fetch even when clicked twice before resolve', async () => {
	const guard = createSubmitGuard();
	let calls = 0;
	const slow = guard.run(async () => {
		calls += 1;
		await new Promise((resolve) => setTimeout(resolve, 20));
		return 'done';
	});
	const skipped = guard.run(async () => {
		calls += 1;
		return 'skipped';
	});
	await Promise.all([slow, skipped]);
	assert.equal(calls, 1);
});

test('challenge form is not shown on first paint of password mode', () => {
	assert.equal(initialAuthView(''), 'password');
	assert.equal(shouldShowChallenge('password'), false);
});

test('challenge form is shown when totp is required in the query', () => {
	assert.equal(initialAuthView('?totp=required'), 'challenge');
	assert.equal(shouldShowChallenge('challenge'), true);
});

test('join reads the invite token from the hash, not search params', () => {
	assert.equal(readInviteTokenFromHash('#invite-token-abc'), 'invite-token-abc');
	assert.equal(readInviteTokenFromHash(''), null);
});

test('wiring: join route reads location.hash', () => {
	assert.match(joinSource, /location\.hash/);
	assert.match(joinSource, /joinGuard\.run/);
	assert.doesNotMatch(joinSource, /location\.search/);
});

test('wiring: login submit uses the shared guard', () => {
	const loginSource = readFileSync(join(here, '../routes/login/+page.svelte'), 'utf8');
	assert.match(loginSource, /createSubmitGuard/);
	assert.match(loginSource, /loginGuard\.run/);
	assert.match(loginSource, /apiFetch/);
	assert.match(loginSource, /PUBLIC_SITE_MODE/);
	assert.match(loginSource, /!landing &&/);
	assert.match(loginSource, /let view = \$state<AuthView>\('password'\)/);
	assert.match(loginSource, /view = initialAuthView\(page\.url\.search\)/);
	assert.doesNotMatch(
		loginSource,
		/\$state<AuthView>\(initialAuthView\(page\.url\.search\)\)/
	);
});
