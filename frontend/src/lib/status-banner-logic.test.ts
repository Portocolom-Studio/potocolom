import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
	adminBannerPutBody,
	bannerDefaultMessageKey,
	bannerDismissKey,
	bannerText,
	bannerTone,
	bannerVisible,
	type BannerConfig
} from './status-banner-logic.ts';

test('no banner is never visible', () => {
	assert.equal(bannerVisible(null, null), false);
	assert.equal(bannerVisible(null, 'anything'), false);
});

test('an unset banner is visible until its own key is dismissed', () => {
	const banner: BannerConfig = {
		kind: 'high_demand',
		message_key: 'app.banner.high_demand',
		custom_text: null
	};
	assert.equal(bannerVisible(banner, null), true);
	assert.equal(bannerVisible(banner, 'something else'), true);
	assert.equal(bannerVisible(banner, bannerDismissKey(banner)), false);
});

test('a changed banner is visible again even though something was dismissed', () => {
	const first: BannerConfig = { kind: 'degraded', message_key: null, custom_text: 'Slow today.' };
	const dismissed = bannerDismissKey(first);
	const second: BannerConfig = { kind: 'degraded', message_key: null, custom_text: 'Slower.' };
	assert.equal(bannerVisible(second, dismissed), true);
});

test('custom text and a message key produce different dismiss keys', () => {
	const key: BannerConfig = {
		kind: 'maintenance',
		message_key: 'app.banner.maintenance',
		custom_text: null
	};
	const custom: BannerConfig = {
		kind: 'maintenance',
		message_key: null,
		custom_text: 'Back soon.'
	};
	assert.notEqual(bannerDismissKey(key), bannerDismissKey(custom));
});

test('high_demand is muted, degraded and maintenance are destructive', () => {
	assert.equal(bannerTone('high_demand'), 'muted');
	assert.equal(bannerTone('degraded'), 'destructive');
	assert.equal(bannerTone('maintenance'), 'destructive');
});

test('the default message choice sends the kind default key and no custom text', () => {
	assert.deepEqual(adminBannerPutBody('high_demand', 'default', 'ignored'), {
		kind: 'high_demand',
		message_key: bannerDefaultMessageKey('high_demand'),
		custom_text: null
	});
});

test('the custom message choice sends trimmed text and no key', () => {
	assert.deepEqual(adminBannerPutBody('degraded', 'custom', '  Slower than usual.  '), {
		kind: 'degraded',
		message_key: null,
		custom_text: 'Slower than usual.'
	});
});

test('banner text is the custom text, else the translation, and empty for an unknown key', () => {
	const known: Record<string, string> = { 'app.banner.degraded': 'Slower than usual.' };
	const translate = (key: string) => known[key] ?? key;
	assert.equal(bannerText(null, translate), '');
	assert.equal(
		bannerText({ kind: 'degraded', message_key: null, custom_text: 'Custom.' }, translate),
		'Custom.'
	);
	assert.equal(
		bannerText(
			{ kind: 'degraded', message_key: 'app.banner.degraded', custom_text: '' },
			translate
		),
		'Slower than usual.'
	);
	assert.equal(
		bannerText({ kind: 'maintenance', message_key: null, custom_text: null }, translate),
		'',
		'a key with no translation must not reach the page'
	);
});
