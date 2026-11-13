#!/usr/bin/env node
import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import { access, readFile, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';
import { recordPageEvents } from './studio-test-page.mjs';

// Runs against the self-hosted build (PUBLIC_SITE_MODE empty): the studio shell
// fills the first viewport, the page does not scroll, no page footer renders, and
// home sends the browser to /app without leaving / in the history.
const build = resolve(fileURLToPath(new URL('../build', import.meta.url)));
const contentTypes = {
	'.html': 'text/html',
	'.js': 'application/javascript',
	'.css': 'text/css',
	'.json': 'application/json',
	'.svg': 'image/svg+xml',
	'.png': 'image/png',
	'.webp': 'image/webp',
	'.woff2': 'font/woff2',
	'.ico': 'image/x-icon'
};

async function chromeExecutable() {
	for (const candidate of [
		process.env.PUPPETEER_EXECUTABLE_PATH,
		process.env.CHROME_PATH,
		process.env.CHROME_BIN,
		'/usr/bin/google-chrome',
		'/usr/bin/chromium',
		'/usr/bin/chromium-browser'
	].filter(Boolean)) {
		if (
			await access(candidate, constants.X_OK).then(
				() => true,
				() => false
			)
		)
			return candidate;
	}
	throw new Error('No Chrome executable found; set PUPPETEER_EXECUTABLE_PATH or CHROME_PATH.');
}

async function serveBuild() {
	const server = createServer(async (request, response) => {
		try {
			const pathname = new URL(request.url, 'http://localhost').pathname;
			const clean = resolve(build, `.${decodeURIComponent(pathname)}`);
			if (clean !== build && !clean.startsWith(build + '/')) {
				response.writeHead(404).end();
				return;
			}
			let found;
			for (const candidate of [
				clean,
				clean + '.html',
				join(clean, 'index.html'),
				join(build, '404.html')
			]) {
				if (
					await stat(candidate).then(
						(value) => value.isFile(),
						() => false
					)
				) {
					found = candidate;
					break;
				}
			}
			if (!found) {
				response.writeHead(404).end();
				return;
			}
			response.writeHead(200, {
				'Content-Type': contentTypes[extname(found)] ?? 'application/octet-stream'
			});
			response.end(await readFile(found));
		} catch (error) {
			response.writeHead(500).end(String(error));
		}
	});
	await new Promise((resolveListen, reject) => {
		server.once('error', reject);
		server.listen(0, '127.0.0.1', resolveListen);
	});
	return server;
}

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
let browser;
try {
	server = await serveBuild();
	const baseUrl = `http://127.0.0.1:${server.address().port}`;

	browser = await puppeteer.launch({
		executablePath: await chromeExecutable(),
		headless: true,
		args: ['--no-sandbox', '--disable-gpu']
	});
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
	await scripted.goto(`${baseUrl}/no-such-page`, { waitUntil: 'networkidle0' });
	const [english] = await notFoundHeadings();
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
