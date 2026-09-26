import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mayReadSessions } from './benchmark.ts';

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
