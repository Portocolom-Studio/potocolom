import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	canUnlink,
	canUnlinkIdentity,
	deleteConfirmed,
	isFollowableRedirect,
	linkableProviders,
	signInAgainHref
} from './account-logic.ts';
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
	// The path half comes from resolve('/login'), which the test loader stubs
	// as the identity, so the built link is the literal login path plus the
	// encoded address of this view.
	assert.equal(href, '/login?next=' + encodeURIComponent('/app?view=account'));
	const next = new URL(href, 'https://studio.test').searchParams.get('next');
	assert.equal(next, '/app?view=account');
	// The login page only returns to a same-origin /app address, so the link it
	// hands back must still name this view.
	assert.equal(studioReturnSearch(next), '?view=account');
});

test('linkableProviders offers the installed providers the account does not have', () => {
	assert.deepEqual(linkableProviders(['password', 'google', 'github'], []), ['google', 'github']);
	assert.deepEqual(linkableProviders(['password', 'google', 'github'], ['github']), ['google']);
	assert.deepEqual(linkableProviders(['password', 'google'], ['google']), []);
	assert.deepEqual(linkableProviders([], ['password']), []);
});

test('linkableProviders never offers a password or an identity already linked', () => {
	// A password is not a provider to send anybody to, and a second copy of
	// an identity the account already holds is not linking anything.
	assert.deepEqual(linkableProviders(['password'], ['password']), []);
	assert.deepEqual(linkableProviders(['password', 'google'], ['password', 'google']), []);
	assert.deepEqual(linkableProviders(['google'], ['google', 'github']), []);
});

test('linkableProviders keeps the order the install offers them in', () => {
	assert.deepEqual(linkableProviders(['github', 'google', 'password'], ['google']), ['github']);
	assert.notDeepEqual(
		linkableProviders(['github', 'google'], ['password']),
		linkableProviders(['google', 'github'], ['password'])
	);
});

test('canUnlink is true while more than one identity is left', () => {
	assert.equal(canUnlink([]), false);
	assert.equal(canUnlink(['password']), false);
	assert.equal(canUnlink(['google']), false);
	assert.equal(canUnlink(['password', 'google']), true);
	assert.equal(canUnlink(['google', 'github', 'password']), true);
});

test('canUnlinkIdentity never offers the password row', () => {
	// The server accepts no provider outside its unlinkable list, and a
	// password is not on it, so the control is absent whatever else is linked.
	assert.equal(canUnlinkIdentity('password', ['password']), false);
	assert.equal(canUnlinkIdentity('password', ['password', 'github']), false);
	assert.equal(canUnlinkIdentity('password', ['password', 'google', 'github']), false);
});

test('canUnlinkIdentity follows canUnlink for the other providers', () => {
	// One identity left is the last way in, whatever provider it is.
	assert.equal(canUnlinkIdentity('google', ['google']), false);
	assert.equal(canUnlinkIdentity('github', ['github']), false);
	// Two identities: the provider that is not the password may go.
	assert.equal(canUnlinkIdentity('google', ['password', 'google']), true);
	assert.equal(canUnlinkIdentity('github', ['password', 'github']), true);
	assert.equal(canUnlinkIdentity('google', ['google', 'github']), true);
});

test('isFollowableRedirect follows https addresses anywhere', () => {
	assert.equal(
		isFollowableRedirect('https://provider.example/redirect', 'http://localhost:5173'),
		true
	);
	assert.equal(isFollowableRedirect('https://studio.example/app', 'https://studio.example'), true);
});

test('isFollowableRedirect follows http only on the page origin', () => {
	assert.equal(isFollowableRedirect('http://localhost:5173/next', 'http://localhost:5173'), true);
	assert.equal(
		isFollowableRedirect('http://provider.example/next', 'http://localhost:5173'),
		false
	);
	assert.equal(isFollowableRedirect('http://localhost:5173/next', 'https://studio.example'), false);
});

test('isFollowableRedirect refuses schemes that are not web addresses', () => {
	assert.equal(isFollowableRedirect('javascript:alert(1)', 'http://localhost:5173'), false);
	assert.equal(
		isFollowableRedirect('data:text/html,<script></script>', 'http://localhost:5173'),
		false
	);
});

test('isFollowableRedirect refuses anything that does not parse as a URL', () => {
	assert.equal(isFollowableRedirect('', 'http://localhost:5173'), false);
	assert.equal(isFollowableRedirect('not a url', 'http://localhost:5173'), false);
	assert.equal(isFollowableRedirect('/relative/path', 'http://localhost:5173'), false);
});
