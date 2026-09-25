import assert from 'node:assert/strict';
import { test } from 'node:test';

import { accountInitial, accountRoleLabelKey } from './account-display.ts';

test('account initial is the first letter of the email, uppercased', () => {
	assert.equal(accountInitial('ada@example.com'), 'A');
	assert.equal(accountInitial('Ziggy@example.com'), 'Z');
});

test('account initial trims leading whitespace before reading the first letter', () => {
	assert.equal(accountInitial('  bo@example.com'), 'B');
});

test('each role maps to its own i18n label key', () => {
	assert.equal(accountRoleLabelKey('admin'), 'app.shell.role_admin');
	assert.equal(accountRoleLabelKey('user'), 'app.shell.role_user');
	assert.equal(accountRoleLabelKey('viewer'), 'app.shell.role_viewer');
});
