import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	createSubmitGuard,
	initialAuthView,
	readInviteTokenFromHash,
	resetJustHappened,
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

test('resetJustHappened reads the reset-done flag from the login query', () => {
	assert.equal(resetJustHappened(''), false);
	assert.equal(resetJustHappened('?reset=done'), true);
	assert.equal(resetJustHappened('?reset=other'), false);
	assert.equal(resetJustHappened('?totp=required&reset=done'), true);
});

test('wiring: join route reads location.hash', () => {
	assert.match(joinSource, /location\.hash/);
	assert.match(joinSource, /joinGuard\.run/);
	assert.doesNotMatch(joinSource, /location\.search/);
});

test('wiring: join follows a hashchange and removes the listener on unmount', () => {
	assert.match(joinSource, /readInviteTokenFromHash\(location\.hash\)/);
	assert.match(joinSource, /addEventListener\('hashchange'/);
	assert.match(joinSource, /removeEventListener\('hashchange'/);
});

test('wiring: a hashchange clears the join form so one link never inherits another', () => {
	assert.match(
		joinSource,
		/function onHashChange\(\) \{[\s\S]*?password = '';[\s\S]*?confirmPassword = '';/
	);
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
	assert.doesNotMatch(loginSource, /\$state<AuthView>\(initialAuthView\(page\.url\.search\)\)/);
});

test('wiring: login links to the reset route and reads the reset-done flag', () => {
	const loginSource = readFileSync(join(here, '../routes/login/+page.svelte'), 'utf8');
	assert.match(loginSource, /auth\.login\.forgot/);
	assert.match(loginSource, /auth\.login\.reset_done/);
	assert.match(loginSource, /resetJustHappened/);
	assert.match(loginSource, /resolve\('\/reset'\)/);
});

test('wiring: reset and recover share one component, are not public, and stay out of the sitemap', () => {
	const resetSource = readFileSync(join(here, '../routes/reset/+page.svelte'), 'utf8');
	const recoverSource = readFileSync(join(here, '../routes/recover/+page.svelte'), 'utf8');
	const sitemapSource = readFileSync(join(here, '../routes/sitemap.xml/+server.ts'), 'utf8');
	assert.match(resetSource, /PUBLIC_SITE_MODE/);
	assert.match(resetSource, /<ResetFlow \/>/);
	assert.match(recoverSource, /PUBLIC_SITE_MODE/);
	assert.match(recoverSource, /<ResetFlow recovery \/>/);
	assert.doesNotMatch(sitemapSource, /\/reset/);
	assert.doesNotMatch(sitemapSource, /\/recover/);
});

test('wiring: the shared reset flow reads the token from the hash and follows a hashchange', () => {
	const flowSource = readFileSync(join(here, 'components/reset-flow.svelte'), 'utf8');
	assert.match(flowSource, /readInviteTokenFromHash\(location\.hash\)/);
	assert.match(flowSource, /addEventListener\('hashchange'/);
	assert.match(flowSource, /removeEventListener\('hashchange'/);
});

test('wiring: the ask form posts the address and answers one neutral message', () => {
	const flowSource = readFileSync(join(here, 'components/reset-flow.svelte'), 'utf8');
	assert.match(flowSource, /\/api\/v1\/auth\/reset'/);
	assert.match(flowSource, /response\.status === 202/);
	assert.match(flowSource, /auth\.reset\.asked/);
});

test('wiring: the complete form handles success, a spent link, a mismatch and a weak password', () => {
	const flowSource = readFileSync(join(here, 'components/reset-flow.svelte'), 'utf8');
	assert.match(flowSource, /\/api\/v1\/auth\/reset\/complete'/);
	assert.match(flowSource, /response\.status === 204/);
	assert.match(flowSource, /reset=done/);
	assert.match(flowSource, /goto\([\s\S]*?\?reset=done[\s\S]*?replaceState: true/);
	assert.match(flowSource, /parsed\.kind === 'policy'/);
	assert.match(flowSource, /auth\.reset\.policy/);
	assert.match(flowSource, /parsed\.kind === 'invalid'/);
	assert.match(flowSource, /auth\.reset\.invalid/);
	assert.match(flowSource, /password !== confirmPassword/);
	assert.match(flowSource, /auth\.reset\.password_mismatch/);
	assert.match(flowSource, /auth\.reset\.ask_new/);
});

test('wiring: a hashchange clears the password fields so one link never inherits another', () => {
	const flowSource = readFileSync(join(here, 'components/reset-flow.svelte'), 'utf8');
	assert.match(
		flowSource,
		/function onHashChange\(\) \{[\s\S]*?password = '';[\s\S]*?confirmPassword = '';/
	);
});

test('wiring: the neutral asked answer carries role="status" so a screen reader announces it', () => {
	const flowSource = readFileSync(join(here, 'components/reset-flow.svelte'), 'utf8');
	assert.match(flowSource, /<Card\.Description role="status">[\s\S]*?auth\.reset\.asked/);
});

test('wiring: generate panel posts use apiFetch', () => {
	const generateSource = readFileSync(join(here, 'components/generate-panel.svelte'), 'utf8');
	assert.equal([...generateSource.matchAll(/apiFetch\('\/api\/v1\/generations'/g)].length, 2);
	assert.doesNotMatch(generateSource, /(?<!api)fetch\('\/api\/v1\/generations'/);
});

test('wiring: studio star writes use apiFetch', () => {
	const studioSource = readFileSync(join(here, 'studio.svelte.ts'), 'utf8');
	assert.equal(
		[...studioSource.matchAll(/apiFetch\(`\/api\/v1\/generations\/\$\{id\}\/star`/g)].length,
		2
	);
	assert.doesNotMatch(studioSource, /(?<!api)fetch\(`\/api\/v1\/generations\/\$\{id\}\/star`/);
});
