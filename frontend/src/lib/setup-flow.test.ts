import assert from 'node:assert/strict';
import { test } from 'node:test';

import { setupRequiredFrom, setupStepFrom, setupTokenFromHash } from './setup-flow.ts';

const token = 'Qx7_vB2-kL9mN4pR8sT1uW3yZ6aC0dE5fG7hJ9iK2lM'; // gitleaks:allow (made-up fixture token, not a credential)

test('the setup token is the hash without its marker', () => {
	assert.equal(setupTokenFromHash(`#${token}`), token);
	assert.equal(setupTokenFromHash(token), token);
	assert.equal(setupTokenFromHash(`  #${token}\n`), token);
});

test('an empty hash has no token', () => {
	assert.equal(setupTokenFromHash(''), null);
	assert.equal(setupTokenFromHash('#'), null);
	assert.equal(setupTokenFromHash('   '), null);
});

test('a hash that is not token-shaped has no token', () => {
	assert.equal(setupTokenFromHash('#abc'), null);
	assert.equal(setupTokenFromHash(`#${token}&next=/app`), null);
	assert.equal(setupTokenFromHash(`#${token}/extra`), null);
	assert.equal(setupTokenFromHash(`#${'a'.repeat(300)}`), null);
	assert.equal(setupTokenFromHash('#<script>'), null);
});

test('setup is required only when config says so', () => {
	assert.equal(setupRequiredFrom({ setup_required: true }), true);
	assert.equal(setupRequiredFrom({ setup_required: false }), false);
	assert.equal(setupRequiredFrom({ auth_methods: [] }), false);
	assert.equal(setupRequiredFrom({ setup_required: 'true' }), false);
	assert.equal(setupRequiredFrom(null), false);
	assert.equal(setupRequiredFrom('setup_required'), false);
});

test('an unclaimed install offers the choice of accounts or none', () => {
	assert.equal(setupStepFrom({ setup_required: true, auth_methods: [] }), 'choose');
});

test('an install an override decided as accounts gets the admin claim alone', () => {
	assert.equal(
		setupStepFrom({ setup_required: false, auth_methods: ['password', 'google'] }),
		'claim'
	);
});

test('an install an override decided as none is already set up', () => {
	assert.equal(setupStepFrom({ setup_required: false, auth_methods: [] }), 'decided');
	assert.equal(setupStepFrom({ setup_required: false }), 'decided');
	assert.equal(setupStepFrom(null), 'decided');
	assert.equal(setupStepFrom({ setup_required: false, auth_methods: 'password' }), 'decided');
});
