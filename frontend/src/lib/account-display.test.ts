import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	accountInitial,
	accountRoleLabelKey,
	openViewFor,
	accountOwnerId,
	parseAccount,
	sectionNeeds,
	type Capability,
	type GatedSection,
	type Role
} from './account-display.ts';

function bundle(role: Role): ReadonlySet<Capability> {
	const parsed = parseAccount({ email: 'a@example.com', role });
	if (!parsed) throw new Error(role);
	return parsed.capabilities;
}

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

test('parseAccount keeps a well-formed account and refuses anything else', () => {
	assert.deepEqual(parseAccount({ email: 'a@example.com', role: 'viewer', id: 'x' }), {
		email: 'a@example.com',
		role: 'viewer',
		capabilities: new Set()
	});
	assert.equal(parseAccount([]), null);
	assert.equal(parseAccount(null), null);
	assert.equal(parseAccount({ email: '', role: 'admin' }), null);
	assert.equal(parseAccount({ email: 'a@example.com', role: 'toString' })?.capabilities.size, 0);
	assert.equal(parseAccount({ email: 'a@example.com' }), null);
});

test('a legacy account without capabilities derives the known role bundle', () => {
	assert.deepEqual(bundle('user'), new Set(['generate', 'edit_own_work', 'view_own_usage']));
	assert.equal(bundle('admin').has('administer'), true);
	assert.equal(bundle('viewer').size, 0);
});

test('a known role with an empty capability list cannot generate', () => {
	const parsed = parseAccount({ email: 'a@example.com', role: 'admin', capabilities: [] });
	assert.ok(parsed);
	assert.equal(parsed.capabilities.size, 0);
	assert.equal(sectionNeeds('generate', parsed.capabilities), 'user');
	assert.equal(openViewFor('generate', 'usage', parsed.capabilities), 'images');
});

test('an unknown role with a valid id holds no generate and is not the local-mode path', () => {
	const parsed = parseAccount({
		email: 'a@example.com',
		role: 'billing',
		id: '550e8400-e29b-41d4-a716-446655440000'
	});
	assert.ok(parsed);
	assert.equal(parsed.capabilities.size, 0);
	assert.equal(sectionNeeds('generate', parsed.capabilities), 'user');
	assert.equal(openViewFor('generate', 'usage', parsed.capabilities), 'images');
	assert.notEqual(parsed.capabilities, null);
});

test('a billing-shaped response keeps view_own_usage and drops anything unnamed', () => {
	const parsed = parseAccount({
		email: 'a@example.com',
		role: 'billing',
		capabilities: ['view_own_usage', 'not-a-capability']
	});
	assert.ok(parsed);
	assert.deepEqual(parsed.capabilities, new Set(['view_own_usage']));
	assert.equal(sectionNeeds('metrics_mine', parsed.capabilities), null);
	assert.equal(sectionNeeds('generate', parsed.capabilities), 'user');
	assert.equal(sectionNeeds('admin', parsed.capabilities), 'admin');
	assert.equal(openViewFor('metrics', 'mine', parsed.capabilities), 'metrics');
	assert.equal(openViewFor('generate', 'usage', parsed.capabilities), 'images');
});

test('accountOwnerId accepts only a UUID and does not change role parsing', () => {
	const body = {
		email: 'a@example.com',
		role: 'viewer',
		id: '550e8400-e29b-41d4-a716-446655440000'
	};
	assert.equal(accountOwnerId(body), body.id);
	assert.equal(parseAccount({ ...body, id: 'malformed' })?.role, 'viewer');
	assert.equal(accountOwnerId({ ...body, id: 'malformed' }), null);
	assert.equal(accountOwnerId(null), null);
});

test('sectionNeeds marks user-only sections for a viewer only', () => {
	const userSections: GatedSection[] = [
		'generate',
		'image_to_image',
		'upscale',
		'edit_image',
		'image_to_text',
		'realtime_canvas',
		'metrics_mine'
	];
	for (const section of userSections) {
		assert.equal(sectionNeeds(section, bundle('viewer')), 'user');
		assert.equal(sectionNeeds(section, bundle('user')), null);
		assert.equal(sectionNeeds(section, bundle('admin')), null);
	}
});

test('sectionNeeds marks admin-only sections for a viewer or a user', () => {
	const adminSections: GatedSection[] = ['metrics_usage', 'metrics_benchmarks', 'admin'];
	for (const section of adminSections) {
		assert.equal(sectionNeeds(section, bundle('viewer')), 'admin');
		assert.equal(sectionNeeds(section, bundle('user')), 'admin');
		assert.equal(sectionNeeds(section, bundle('admin')), null);
	}
});

test('sectionNeeds marks nothing when there is no account to gate', () => {
	assert.equal(sectionNeeds('generate', null), null);
	assert.equal(sectionNeeds('metrics_usage', null), null);
});

test('openViewFor sends a viewer from generate to images', () => {
	assert.equal(openViewFor('generate', 'usage', bundle('viewer')), 'images');
});

test('openViewFor sends a viewer from metrics to images', () => {
	assert.equal(openViewFor('metrics', 'usage', bundle('viewer')), 'images');
	assert.equal(openViewFor('metrics', 'benchmarks', bundle('viewer')), 'images');
	assert.equal(openViewFor('metrics', 'mine', bundle('viewer')), 'images');
});

test('openViewFor sends a user from metrics to generate', () => {
	assert.equal(openViewFor('metrics', 'usage', bundle('user')), 'generate');
	assert.equal(openViewFor('metrics', 'benchmarks', bundle('user')), 'generate');
});

test('openViewFor keeps a user on My usage, the one metrics tab they own', () => {
	assert.equal(openViewFor('metrics', 'mine', bundle('user')), 'metrics');
});

test('openViewFor keeps a user on upscale', () => {
	assert.equal(openViewFor('upscale', 'usage', bundle('user')), 'upscale');
});

test('openViewFor keeps an admin on metrics', () => {
	assert.equal(openViewFor('metrics', 'usage', bundle('admin')), 'metrics');
	assert.equal(openViewFor('metrics', 'benchmarks', bundle('admin')), 'metrics');
	assert.equal(openViewFor('metrics', 'mine', bundle('admin')), 'metrics');
});

test('openViewFor sends a viewer or member away from every admin tab', () => {
	for (const tab of ['users', 'audit', 'fleet'] as const) {
		assert.equal(openViewFor('admin', tab, bundle('viewer')), 'images');
		assert.equal(openViewFor('admin', tab, bundle('user')), 'generate');
		assert.equal(openViewFor('admin', tab, bundle('admin')), 'admin');
	}
});

test('openViewFor keeps models and images open to a viewer', () => {
	assert.equal(openViewFor('models', 'usage', bundle('viewer')), 'models');
	assert.equal(openViewFor('images', 'usage', bundle('viewer')), 'images');
});

test('openViewFor keeps the account view for every signed-in role', () => {
	for (const role of ['viewer', 'user', 'admin'] as const) {
		assert.equal(openViewFor('account', 'usage', bundle(role)), 'account');
	}
});

test('openViewFor refuses the account view when there is no account to show', () => {
	assert.equal(openViewFor('account', 'usage', null), 'generate');
});

test('openViewFor keeps the account view open in no-accounts mode, for its turn-on card', () => {
	assert.equal(openViewFor('account', 'usage', null, true), 'account');
});

test('openViewFor keeps every view but admin when there is no role', () => {
	for (const view of [
		'generate',
		'image_to_image',
		'upscale',
		'edit_image',
		'image_to_text',
		'realtime_canvas',
		'images',
		'models',
		'metrics'
	] as const) {
		assert.equal(openViewFor(view, 'usage', null), view);
	}
});

test('AUTH_MODE=none shows every section but admin, whose API needs accounts mode', () => {
	assert.equal(sectionNeeds('admin', null), 'admin');
	assert.equal(sectionNeeds('metrics_usage', null), null);
	assert.equal(sectionNeeds('metrics_mine', null), null);
	assert.equal(openViewFor('metrics', 'mine', null), 'metrics');
	assert.equal(openViewFor('admin', 'users', null), 'generate');
});
