import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	buildAdminConfirmation,
	buildAuditQuery,
	modelWorkerAssignments,
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

test('model catalog stays hidden when the API lists manifests without worker assignments', () => {
	assert.deepEqual(
		modelWorkerAssignments([{ id: 'model-a', name: 'Model A', capabilities: ['text_to_image'] }]),
		[]
	);
	assert.deepEqual(
		modelWorkerAssignments([
			{ id: 'model-a', name: 'Model A', worker_ids: ['worker-a', 'worker-b'] }
		]),
		[{ id: 'model-a', name: 'Model A', workerIds: ['worker-a', 'worker-b'] }]
	);
});
