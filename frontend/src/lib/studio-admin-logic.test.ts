import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	adminApiError,
	adminErrorMessage,
	buildAdminConfirmation,
	buildAuditQuery,
	buildUsersQuery,
	invitationExpired,
	inviteRequestBody,
	isRecentAuthenticationRequired,
	needsAdminAttestation
} from './studio-admin-logic.ts';

test('audit filters become only the supported query parameters', () => {
	assert.equal(
		buildAuditQuery({
			actor_user_id: ' actor/id ',
			target_user_id: 'target',
			action: 'user.role',
			limit: '25'
		}),
		'actor_user_id=actor%2Fid&target_user_id=target&action=user.role&limit=25'
	);
	assert.equal(
		buildAuditQuery({ actor_user_id: '', target_user_id: '  ', action: '', limit: '100' }),
		'limit=100'
	);
	assert.equal(
		buildAuditQuery({ actor_user_id: '', target_user_id: '', action: '', limit: 7 }),
		'limit=7'
	);
});

test('confirmation copy names the selected user and the proposed change', () => {
	assert.equal(
		buildAdminConfirmation('Apply {change} to {user}?', 'ada@example.com', 'admin role'),
		'Apply admin role to ada@example.com?'
	);
});

test('admin promotion requires attestation only for an unverified address', () => {
	assert.equal(needsAdminAttestation('admin', false), true);
	assert.equal(needsAdminAttestation('admin', true), false);
	assert.equal(needsAdminAttestation('user', false), false);
	assert.equal(needsAdminAttestation('user', true), false);
	assert.equal(needsAdminAttestation('viewer', false), false);
	assert.equal(needsAdminAttestation('viewer', true), false);
});

test('the users query carries a trimmed, capped q and the page cursor', () => {
	assert.equal(buildUsersQuery('', null), '?limit=50');
	assert.equal(buildUsersQuery('  ada  ', null), '?limit=50&q=ada');
	assert.equal(buildUsersQuery('ada', 'cursor-id'), '?limit=50&q=ada&cursor=cursor-id');
	assert.equal(buildUsersQuery('x'.repeat(250), null), `?limit=50&q=${'x'.repeat(200)}`);
});

test('a string detail passes through unchanged', () => {
	assert.equal(adminErrorMessage('not found', 'fallback'), 'not found');
});

test('an array detail (FastAPI validation) reads the first item msg', () => {
	assert.equal(
		adminErrorMessage([{ msg: 'limit must be at least 1' }, { msg: 'ignored' }], 'fallback'),
		'limit must be at least 1'
	);
});

test('an unreadable detail falls back to the response status text', () => {
	assert.equal(adminErrorMessage(undefined, 'fallback'), 'fallback');
	assert.equal(adminErrorMessage([], 'fallback'), 'fallback');
	assert.equal(adminErrorMessage([{}], 'fallback'), 'fallback');
	assert.equal(adminErrorMessage([{ msg: 42 }], 'fallback'), 'fallback');
});

test('the invite request body trims the address and keeps the chosen role', () => {
	assert.deepEqual(inviteRequestBody('  ada@example.com  ', 'admin'), {
		email: 'ada@example.com',
		role: 'admin'
	});
	assert.deepEqual(inviteRequestBody('ada@example.com', 'viewer'), {
		email: 'ada@example.com',
		role: 'viewer'
	});
});

test('only the recent-authentication detail triggers the hint', () => {
	assert.equal(isRecentAuthenticationRequired('recent authentication required'), true);
	assert.equal(isRecentAuthenticationRequired('that address already has an account'), false);
	assert.equal(isRecentAuthenticationRequired(undefined), false);
});

test('an invitation is expired at or after its expiry, and an unreadable date is not', () => {
	const at = Date.parse('2026-10-05T12:00:00Z');
	assert.equal(invitationExpired('2026-10-05T12:00:00Z', at - 1), false);
	assert.equal(invitationExpired('2026-10-05T12:00:00Z', at), true);
	assert.equal(invitationExpired('not a date', at), false);
});

test('a failed admin response reads its string detail as the message', async () => {
	const response = new Response(JSON.stringify({ detail: 'not allowed here' }), {
		status: 400,
		statusText: 'Bad Request'
	});
	assert.equal(await adminApiError(response), 'not allowed here');
});

test('a failed admin response without a JSON body reads the status text', async () => {
	const response = new Response('<html>proxy error</html>', {
		status: 502,
		statusText: 'Bad Gateway'
	});
	assert.equal(await adminApiError(response), 'Bad Gateway');
});

test('only a 403 from an admin route starts the access recheck', async () => {
	const original = globalThis.fetch;
	const requested: string[] = [];
	try {
		// A 500 from the account route is the quiet "failed" probe result, so the
		// recheck ends without changing any account or view-owner state.
		globalThis.fetch = (async (input) => {
			requested.push(String(input));
			return new Response('', { status: 500 });
		}) as typeof fetch;

		const forbidden = new Response(JSON.stringify({ detail: 'forbidden' }), { status: 403 });
		assert.equal(await adminApiError(forbidden), 'forbidden');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(requested, ['/api/v1/account']);

		requested.length = 0;
		const badRequest = new Response('', { status: 400, statusText: 'Bad Request' });
		assert.equal(await adminApiError(badRequest), 'Bad Request');
		const serverError = new Response('', { status: 500, statusText: 'Internal Server Error' });
		assert.equal(await adminApiError(serverError), 'Internal Server Error');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(requested, []);
	} finally {
		globalThis.fetch = original;
	}
});
