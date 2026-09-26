import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	accountInitial,
	accountRoleLabelKey,
	openViewFor,
	parseAccount,
	sectionNeeds,
	type GatedSection
} from './account-display.ts';

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
		role: 'viewer'
	});
	assert.equal(parseAccount([]), null);
	assert.equal(parseAccount(null), null);
	assert.equal(parseAccount({ email: '', role: 'admin' }), null);
	assert.equal(parseAccount({ email: 'a@example.com', role: 'member' }), null);
	assert.equal(parseAccount({ email: 'a@example.com', role: 'toString' }), null);
});

test('sectionNeeds marks user-only sections for a viewer only', () => {
	const userSections: GatedSection[] = [
		'generate',
		'image_to_image',
		'upscale',
		'edit_image',
		'image_to_text',
		'realtime_canvas'
	];
	for (const section of userSections) {
		assert.equal(sectionNeeds(section, 'viewer'), 'user');
		assert.equal(sectionNeeds(section, 'user'), null);
		assert.equal(sectionNeeds(section, 'admin'), null);
	}
});

test('sectionNeeds marks admin-only sections for a viewer or a user', () => {
	const adminSections: GatedSection[] = ['metrics_usage', 'metrics_benchmarks'];
	for (const section of adminSections) {
		assert.equal(sectionNeeds(section, 'viewer'), 'admin');
		assert.equal(sectionNeeds(section, 'user'), 'admin');
		assert.equal(sectionNeeds(section, 'admin'), null);
	}
});

test('sectionNeeds marks nothing when there is no account to gate', () => {
	assert.equal(sectionNeeds('generate', null), null);
	assert.equal(sectionNeeds('metrics_usage', null), null);
});

test('openViewFor sends a viewer from generate to images', () => {
	assert.equal(openViewFor('generate', 'usage', 'viewer'), 'images');
});

test('openViewFor sends a viewer from metrics to images', () => {
	assert.equal(openViewFor('metrics', 'usage', 'viewer'), 'images');
	assert.equal(openViewFor('metrics', 'benchmarks', 'viewer'), 'images');
});

test('openViewFor sends a user from metrics to generate', () => {
	assert.equal(openViewFor('metrics', 'usage', 'user'), 'generate');
	assert.equal(openViewFor('metrics', 'benchmarks', 'user'), 'generate');
});

test('openViewFor keeps a user on upscale', () => {
	assert.equal(openViewFor('upscale', 'usage', 'user'), 'upscale');
});

test('openViewFor keeps an admin on metrics', () => {
	assert.equal(openViewFor('metrics', 'usage', 'admin'), 'metrics');
	assert.equal(openViewFor('metrics', 'benchmarks', 'admin'), 'metrics');
});

test('openViewFor keeps models and images open to a viewer', () => {
	assert.equal(openViewFor('models', 'usage', 'viewer'), 'models');
	assert.equal(openViewFor('images', 'usage', 'viewer'), 'images');
});

test('openViewFor keeps every view when there is no role', () => {
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
