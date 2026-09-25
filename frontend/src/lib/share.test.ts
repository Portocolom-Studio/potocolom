import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	ShareGoneError,
	downloadSharedPicture,
	resolveShare,
	shareDownloadName,
	shareResolveStillCurrent
} from './share.ts';

const here = dirname(fileURLToPath(import.meta.url));

function stubFetch(handler: (url: string, init?: RequestInit) => Response) {
	const original = globalThis.fetch;
	globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
		Promise.resolve(handler(String(input), init))) as typeof fetch;
	return () => {
		globalThis.fetch = original;
	};
}

test('resolveShare posts the token and parses the picture', async () => {
	let seen: { url?: string; body?: string } = {};
	const restore = stubFetch((url, init) => {
		seen = { url, body: typeof init?.body === 'string' ? init.body : undefined };
		return new Response(
			JSON.stringify({
				asset: { id: 'asset-1', width: 512, height: 512, mime: 'image/png' },
				prompt: 'a red fox',
				model: 'sd-sim',
				url: 'http://localhost:8441/api/v1/shared-picture?share=abc&expires=1&signature=xyz'
			}),
			{ status: 200, headers: { 'Content-Type': 'application/json' } }
		);
	});
	try {
		const info = await resolveShare('token-123');
		assert.equal(seen.url, '/api/v1/shared');
		assert.equal(seen.body, JSON.stringify({ token: 'token-123' }));
		assert.equal(info.prompt, 'a red fox');
		assert.equal(info.model, 'sd-sim');
		assert.equal(info.asset.width, 512);
		assert.equal(info.asset.height, 512);
		assert.equal(info.asset.mime, 'image/png');
	} finally {
		restore();
	}
});

test('resolveShare maps a revoked, expired or unknown token to gone', async () => {
	const restore = stubFetch(
		() =>
			new Response(JSON.stringify({ detail: 'no such share' }), {
				status: 404,
				headers: { 'Content-Type': 'application/json' }
			})
	);
	try {
		await assert.rejects(resolveShare('made-up-token'), ShareGoneError);
	} finally {
		restore();
	}
});

test('resolveShare keeps any other refusal distinct from gone', async () => {
	const restore = stubFetch(() => new Response('boom', { status: 500 }));
	try {
		await assert.rejects(resolveShare('token'), (error: unknown) => {
			assert.ok(!(error instanceof ShareGoneError));
			return true;
		});
	} finally {
		restore();
	}
});

test('shareDownloadName names the file from the mime', () => {
	assert.equal(shareDownloadName('asset-1', 'image/png'), 'potocolom-asset-1.png');
	assert.equal(shareDownloadName('asset-1', 'image/webp'), 'potocolom-asset-1.webp');
	assert.equal(shareDownloadName('asset-1', 'image/jpeg'), 'potocolom-asset-1.bin');
});

test('shareResolveStillCurrent keeps only an answer for the token the page shows now', () => {
	assert.equal(shareResolveStillCurrent('older', 'older'), true);
	assert.equal(shareResolveStillCurrent('older', 'newer'), false);
	assert.equal(shareResolveStillCurrent('older', null), false);
});

test('downloadSharedPicture re-resolves the token and downloads the fresh address', async () => {
	let resolves = 0;
	let seenBody = '';
	const restore = stubFetch((url, init) => {
		resolves += 1;
		seenBody = typeof init?.body === 'string' ? init.body : '';
		return new Response(
			JSON.stringify({
				asset: { id: 'asset-1', width: 512, height: 512, mime: 'image/png' },
				prompt: null,
				model: null,
				url: 'http://localhost:8441/api/v1/shared-picture?share=abc&expires=99&signature=fresh'
			}),
			{ status: 200, headers: { 'Content-Type': 'application/json' } }
		);
	});
	let clicked = false;
	let removed = false;
	const anchor = {
		href: '',
		download: '',
		click() {
			clicked = true;
		},
		remove() {
			removed = true;
		}
	};
	const doc = {
		createElement(tag: string) {
			assert.equal(tag, 'a');
			return anchor;
		},
		body: { appendChild() {} }
	};
	try {
		await downloadSharedPicture('token-123', doc as unknown as Document);
		assert.equal(resolves, 1, 'a stale address in the page must not be reused');
		assert.equal(seenBody, JSON.stringify({ token: 'token-123' }));
		assert.equal(
			anchor.href,
			'http://localhost:8441/api/v1/shared-picture?share=abc&expires=99&signature=fresh'
		);
		assert.equal(anchor.download, 'potocolom-asset-1.png');
		assert.ok(clicked);
		assert.ok(removed);
	} finally {
		restore();
	}
});

test('downloadSharedPicture surfaces a gone token so the page can show invalid', async () => {
	const restore = stubFetch(
		() =>
			new Response(JSON.stringify({ detail: 'no such share' }), {
				status: 404,
				headers: { 'Content-Type': 'application/json' }
			})
	);
	const doc = {
		createElement() {
			throw new Error('a gone resolve must not touch the document');
		},
		body: { appendChild() {} }
	};
	try {
		await assert.rejects(downloadSharedPicture('gone', doc as unknown as Document), ShareGoneError);
	} finally {
		restore();
	}
});

test('wiring: shared page reads the token from the hash, reacts to hashchange, and cleans up', () => {
	const sharedSource = readFileSync(join(here, '../routes/shared/+page.svelte'), 'utf8');
	assert.match(sharedSource, /location\.hash/);
	assert.match(sharedSource, /hashchange/);
	assert.match(sharedSource, /removeEventListener\('hashchange'/);
	assert.match(sharedSource, /resolveShare/);
	assert.match(sharedSource, /downloadSharedPicture/);
	assert.doesNotMatch(sharedSource, /location\.search/);
});

test('wiring: shared page discards a resolve answer for a token the hash moved on from', () => {
	const sharedSource = readFileSync(join(here, '../routes/shared/+page.svelte'), 'utf8');
	assert.match(sharedSource, /shareResolveStillCurrent\(current, token\)/);
});

test('wiring: shared page gates on the landing build and stays out of the sitemap', () => {
	const sharedSource = readFileSync(join(here, '../routes/shared/+page.svelte'), 'utf8');
	assert.match(sharedSource, /PUBLIC_SITE_MODE === 'landing'/);
	assert.match(sharedSource, /noindex/);
	const sitemap = readFileSync(join(here, '../routes/sitemap.xml/+server.ts'), 'utf8');
	assert.doesNotMatch(sitemap, /\/shared/);
});

test('i18n: shared strings exist in both dictionaries', () => {
	const en = JSON.parse(readFileSync(join(here, 'i18n/en.json'), 'utf8')) as Record<string, string>;
	const es = JSON.parse(readFileSync(join(here, 'i18n/es.json'), 'utf8')) as Record<string, string>;
	const keys = Object.keys(en).filter((key) => key.startsWith('shared.'));
	assert.ok(keys.length > 0);
	for (const key of keys) {
		assert.ok(key in es, `missing ${key} in es.json`);
	}
});
