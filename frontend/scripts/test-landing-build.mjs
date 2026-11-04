import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

test('the built landing /app contains the studio preview', async () => {
	const html = await readFile(new URL('../build/app.html', import.meta.url), 'utf8');
	const body = html.slice(html.indexOf('<body'), html.indexOf('</body>'));
	assert.match(body, /<div\s[^>]*\bdata-studio-preview(?:\s|[=>])/);
});

const built = (name) => readFile(new URL(`../build/${name}.html`, import.meta.url), 'utf8');

test('the landing build prerenders the public-site pages', async () => {
	for (const name of [
		'whitepaper',
		'benchmark',
		'illusions',
		'legal',
		'privacy',
		'hero-preview',
		'collage-preview',
		'docs',
		'docs/guide/introduction'
	]) {
		assert.match(await built(name), /<html\b/, name);
	}
});

test('the landing build prerenders every collage variant document', async () => {
	const source = await readFile(new URL('../src/lib/collage-variants.ts', import.meta.url), 'utf8');
	const variants = [...source.matchAll(/^\t(?:'([^']+)'|(\w[\w-]*)): \{/gm)].map(
		(match) => match[1] ?? match[2]
	);
	assert.ok(variants.includes('masonry') && variants.includes('graph'), 'variant ids');
	for (const id of variants) {
		assert.match(await built(`collage-preview/${id}`), /<html\b/, id);
	}
});

test('built pages share one horizon footer with the same landmarks', async () => {
	for (const name of [
		'index',
		'whitepaper',
		'benchmark',
		'illusions',
		'login',
		'join',
		'setup',
		'reset',
		'recover',
		'shared',
		'legal',
		'privacy',
		'hero-preview',
		'collage-preview'
	]) {
		const html = await built(name);
		assert.equal((html.match(/<footer class="horizon\b/g) ?? []).length, 1, name);
		const footer = html.slice(html.indexOf('<footer class="horizon'), html.indexOf('</footer>'));
		for (const landmark of [
			'class="top ',
			'class="columns ',
			'class="trust ',
			'class="band ',
			'class="wordmark ',
			'class="bottom '
		]) {
			assert.ok(footer.includes(landmark), `${name}: ${landmark}`);
		}
		assert.ok(
			footer.indexOf('class="bottom ') < footer.indexOf('class="band '),
			`${name}: notice above artwork`
		);
		assert.ok(!footer.includes('class="to-top '), `${name}: no back-to-top button`);
	}
});

test('setup prerenders as an unavailable card with no form', async () => {
	const html = await built('setup');
	assert.match(html, /<h1[^>]*>[^<]*Set up[^<]*<\/h1>/);
	assert.ok(html.includes('Sign-in is available on a self-hosted or cloud studio install'));
	assert.doesNotMatch(html, /<form\b/);
	assert.doesNotMatch(html, /id="setup-email"/);
});

test('the studio has no page footer', async () => {
	assert.doesNotMatch(await built('app'), /<footer class="horizon\b/);
});

test('the landing preview has no Show tour launcher', async () => {
	assert.doesNotMatch(await built('app'), /Show tour/);
});

test('headers lead to sign in or the waitlist, and login has no kicker', async () => {
	for (const name of ['index', 'whitepaper']) {
		const html = await built(name);
		const header = html.match(/<header\b[\s\S]*?<\/header>/)?.[0];
		assert.ok(header, name);
		const expected = process.env.PUBLIC_WAITLIST_URL ? './login?tab=waitlist' : './login';
		assert.ok(header.includes(`href="${expected}"`), `${name}: expected ${expected}`);
	}
	assert.doesNotMatch(await built('login'), /class="kicker\b/);
});

test('language controls have named flag buttons', async () => {
	for (const name of ['whitepaper', 'login', 'legal']) {
		const html = await built(name);
		for (const [lang, label] of [
			['en', 'English'],
			['es', 'Espa\u00f1ol']
		]) {
			assert.match(
				html,
				new RegExp(`lang="${lang}" aria-label="${label}" title="${label}"[^>]*>[\\s\\S]*?<svg`)
			);
		}
	}
});

test('the install command uses the copied official redirect', async () => {
	const html = await built('index');
	assert.ok(html.includes('curl -fsSL https://potocolom.leonfuller.com/install.sh | bash'));
	assert.equal(
		await readFile(new URL('../build/_redirects', import.meta.url), 'utf8'),
		'/install.sh https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh 302\n'
	);
});
