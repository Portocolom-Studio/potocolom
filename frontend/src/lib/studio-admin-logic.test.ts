import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	adminErrorMessage,
	buildAdminConfirmation,
	buildAuditQuery,
	buildUsersQuery,
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
