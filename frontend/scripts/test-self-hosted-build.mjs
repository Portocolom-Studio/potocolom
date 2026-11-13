import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import { test } from 'node:test';

// Run against the self-hosted build (PUBLIC_SITE_MODE empty): build/ holds the app only.
const removed = [
	'pricing',
	'whitepaper',
	'benchmark',
	'illusions',
	'privacy',
	'legal',
	'collage-preview',
	'hero-preview',
	'docs'
];
const kept = ['index', 'app', 'login', 'join', 'reset', 'recover', 'shared'];
const documentPages = ['whitepaper', 'benchmark', 'illusions', 'legal', 'privacy', 'pricing'];
const publicSite = 'https://potocolom.leonfuller.com';

const buildDir = new URL('../build/', import.meta.url);
const built = (name) => readFile(new URL(`${name}.html`, buildDir), 'utf8');

// Every file under build/, nested folders included, as paths like collage-preview/masonry.html.
async function listFiles(dir = '') {
	const files = [];
	for (const entry of await readdir(new URL(dir, buildDir), { withFileTypes: true })) {
		const path = dir + entry.name;
		if (entry.isDirectory()) files.push(...(await listFiles(`${path}/`)));
		else files.push(path);
	}
	return files;
}

// Prerendered page names, without .html: index, login, collage-preview/masonry.
const pages = async () =>
	(await listFiles())
		.filter((file) => file.endsWith('.html'))
		.map((file) => file.slice(0, -'.html'.length));

// Collage variant ids, read from the keys of the variant table.
const variantSource = await readFile(
	new URL('../src/lib/collage-variants.ts', import.meta.url),
	'utf8'
);
const variants = [...variantSource.matchAll(/^\t(?:'([^']+)'|(\w[\w-]*)): \{/gm)].map(
	(match) => match[1] ?? match[2]
);

test('the self-hosted build does not serve the public-site pages, nested or not', async () => {
	const names = await pages();
	const served = names.filter((page) =>
		removed.some((name) => page === name || page.startsWith(`${name}/`))
	);
	assert.deepEqual(served, []);
	for (const id of variants) assert.ok(!names.includes(`collage-preview/${id}`), id);
});

test('the self-hosted build keeps the home redirect and the app pages', async () => {
	const names = await pages();
	for (const name of kept) assert.ok(names.includes(name), name);
});

test('the self-hosted home renders no landing page', async () => {
	const html = await built('index');
	assert.doesNotMatch(html, /<footer class="horizon\b/);
	assert.doesNotMatch(html, /class="landing-content\b/);
});

test('self-hosted footer links point at the public site as whole URLs', async () => {
	for (const name of ['login', 'join', 'reset', 'recover', 'shared']) {
		const html = await built(name);
		for (const page of documentPages) {
			assert.ok(html.includes(`href="${publicSite}/${page}"`), `${name}: ${page}`);
		}
		assert.doesNotMatch(html, /leonfuller\.com\.\//, name);
	}
});

test('no self-hosted page links to a public-site page, nested pages included', async () => {
	// Matches /whitepaper, ./privacy, ../legal/, collage-preview/blue and the like.
	// Absolute public-site URLs are not internal links. A static file such as
	// /benchmark/results.json has a dot and is not a page.
	const links = [];
	for (const page of await pages()) {
		const html = await built(page);
		for (const [, href] of html.matchAll(/\shref="([^"]*)"/g)) {
			const path = href
				.split(/[?#]/)[0]
				.replace(/^(\.{1,2}\/|\/)+/, '')
				.replace(/\.html$/, '')
				.replace(/\/+$/, '');
			if (removed.some((name) => path === name || path.startsWith(`${name}/`))) {
				if (!/\.[a-z0-9]+$/i.test(path)) links.push(`${page}: ${href}`);
			}
		}
	}
	assert.deepEqual(links, []);
});
