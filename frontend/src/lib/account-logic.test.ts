import assert from 'node:assert/strict';
import { test } from 'node:test';

import { deleteConfirmed, signInAgainHref } from './account-logic.ts';
import { studioReturnSearch } from './auth-flow.ts';

test('deleteConfirmed accepts the account email as typed', () => {
	assert.equal(deleteConfirmed('ada@example.com', 'ada@example.com'), true);
	assert.equal(deleteConfirmed('Ada@Example.com', 'ada@example.com'), true);
	assert.equal(deleteConfirmed('  ada@example.com\t', 'ada@example.com'), true);
});

test('deleteConfirmed refuses anything that is not the account email', () => {
	assert.equal(deleteConfirmed('', 'ada@example.com'), false);
	assert.equal(deleteConfirmed('ada@example.co', 'ada@example.com'), false);
	assert.equal(deleteConfirmed('bob@example.com', 'ada@example.com'), false);
	assert.equal(deleteConfirmed('ada@example.com ', 'ada@Example.comx'), false);
});

test('signInAgainHref carries the account view back through the login next parameter', () => {
	const href = signInAgainHref();
	assert.equal(href, '/login?next=' + encodeURIComponent('/app?view=account'));
	const next = new URL(href, 'https://studio.test').searchParams.get('next');
	assert.equal(next, '/app?view=account');
	// The login page only returns to a same-origin /app address, so the link it
	// hands back must still name this view.
	assert.equal(studioReturnSearch(next), '?view=account');
});
