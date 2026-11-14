#!/usr/bin/env node
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChrome, serveBuild } from './browser-kit.mjs';
import { recordPageEvents } from './studio-test-page.mjs';

// Runs against the self-hosted build (PUBLIC_SITE_MODE empty): the studio shell
// fills the first viewport, the page does not scroll, no page footer renders, and
// home sends the browser to /app without leaving / in the history.
const build = resolve(fileURLToPath(new URL('../build', import.meta.url)));
// The not-found heading in each language, as the error page shows it.
const notFoundHeadings = () =>
	Promise.all(
		['en', 'es'].map(
			async (locale) =>
				JSON.parse(
					await readFile(new URL(`../src/lib/i18n/${locale}.json`, import.meta.url), 'utf8')
				)['error.title404']
		)
	);

let server;
let baseUrl;
let browser;
try {
	({ server, baseUrl } = await serveBuild(build, { fallback: '404.html' }));

	browser = await launchChrome({ args: ['--disable-gpu'] });
	const studio = await browser.newPage();
	recordPageEvents(studio);
	await studio.setViewport({ width: 1280, height: 900 });
	await studio.goto(`${baseUrl}/app`, { waitUntil: 'networkidle0' });
	await studio.waitForSelector('[data-slot="sidebar-container"]', { visible: true });
	const studioHeight = await studio.$eval(
		'[data-studio-shell] > div',
		(shell) => shell.getBoundingClientRect().height
	);
	assert.equal(studioHeight, 900, 'studio shell fills the initial viewport');
	assert.equal(await studio.$$('footer.horizon').then((items) => items.length), 0);
	const documentHeight = await studio.evaluate(() => document.documentElement.scrollHeight);
	assert.equal(documentHeight, 900, 'studio does not scroll the page');
	await studio.close();

	// Home replaces its own entry with /app, so Back returns to the page before home.
	const home = await browser.newPage();
	recordPageEvents(home);
	await home.goto(`${baseUrl}/login`, { waitUntil: 'networkidle0' });
	await home.goto(`${baseUrl}/`, { waitUntil: 'networkidle0' });
	await home.waitForFunction(() => location.pathname === '/app', { timeout: 10000 });
	await home.goBack({ waitUntil: 'networkidle0' });
	assert.equal(
		await home.evaluate(() => location.pathname),
		'/login',
		'Back returns to the page before home'
	);
	await home.close();

	// A missing URL gets the not-found page. With JavaScript off that is the static
	// 404.html, so its heading and home link must show without any script.
	const plain = await browser.newPage();
	await plain.setJavaScriptEnabled(false);
	await plain.goto(`${baseUrl}/no-such-page`, { waitUntil: 'load' });
	const visibleHeadings = await plain.$$eval('h1', (nodes) =>
		nodes.filter((node) => node.checkVisibility()).map((node) => node.textContent)
	);
	for (const text of await notFoundHeadings()) {
		assert.ok(
			visibleHeadings.some((heading) => heading.includes(text)),
			`JavaScript off heading with ${text}`
		);
	}
	const homeLinks = await plain.$$eval(
		'a[href="/"]',
		(nodes) => nodes.filter((node) => node.checkVisibility() && node.textContent.trim()).length
	);
	assert.ok(homeLinks > 0, 'JavaScript off visible home link');
	await plain.close();

	// With JavaScript on the error page replaces the static markup once it mounts.
	const scripted = await browser.newPage();
	await scripted.goto(`${baseUrl}/no-such-page`);
	const [english] = await notFoundHeadings();
	await scripted.waitForFunction(
		(text) => [...document.querySelectorAll('h1')].some((node) => node.textContent.includes(text)),
		{ timeout: 15000 },
		english
	);
	const matches = await scripted.$$eval(
		'h1',
		(nodes, text) => nodes.filter((node) => node.textContent.includes(text)).length,
		english
	);
	assert.equal(matches, 1, 'one not-found heading after the page mounts');
	assert.equal(await scripted.title(), `potocolom - ${english}`);
	await scripted.close();

	console.log('Studio shell, footer, home redirect and not-found page checks passed.');
} finally {
	if (browser) await browser.close();
	if (server)
		await new Promise((resolveClose, reject) =>
			server.close((error) => (error ? reject(error) : resolveClose()))
		);
}
