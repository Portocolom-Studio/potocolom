#!/usr/bin/env node
import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import { access, readFile, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

// Runs against the landing build (PUBLIC_SITE_MODE=landing npm run build) and the real
// prerendered docs pages. A missing page means the build is stale, so this fails instead
// of letting the static server fall back to the app shell.
const build = resolve(fileURLToPath(new URL('../build', import.meta.url)));
const pages = {
	introduction: '/docs/guide/introduction',
	quickstart: '/docs/guide/quickstart',
	endpoints: '/docs/api/endpoints',
	decisions: '/docs/reference/decisions'
};
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

async function requireBuiltPages() {
	for (const path of Object.values(pages)) {
		const file = join(build, `${path}.html`);
		const html = await readFile(file, 'utf8').catch(() => {
			throw new Error(
				`Docs build missing: ${file}. Run PUBLIC_SITE_MODE=landing npm run build first.`
			);
		});
		assert.match(html, /data-docs-shell/, `${path} must be the prerendered docs shell`);
	}
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
			for (const candidate of [clean, clean + '.html', join(clean, 'index.html')]) {
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

// Each page loads with its own theme stored before the app reads it.
async function openDocs(
	browser,
	baseUrl,
	path,
	{ width = 1280, height = 900, mode = 'dark', touch = false } = {}
) {
	const page = await browser.newPage();
	await page.setViewport({ width, height, hasTouch: touch, isMobile: touch });
	await page.evaluateOnNewDocument((theme) => {
		try {
			localStorage.setItem('landing-mode', theme);
		} catch {
			// Storage blocked: the page uses its default theme for this visit.
		}
	}, mode);
	await page.goto(`${baseUrl}${path}`, { waitUntil: 'networkidle0' });
	await page.waitForSelector('#docs-nav');
	return page;
}

// Waits up to 3s for a condition and reports whether it happened, so a failure reads as an assertion.
const happens = (page, fn, ...args) =>
	page.waitForFunction(fn, { timeout: 3000 }, ...args).then(
		() => true,
		() => false
	);
const drawerOpen = (page) => page.$eval('#docs-nav', (nav) => nav.classList.contains('open'));
const drawerIs = (page, open) =>
	happens(
		page,
		(want) => document.getElementById('docs-nav').classList.contains('open') === want,
		open
	);
const focusOnMenu = (page) =>
	page.evaluate(() => document.activeElement === document.querySelector('.menu-btn'));
const focusInDrawer = (page) =>
	page.evaluate(() => document.getElementById('docs-nav').contains(document.activeElement));
const overflowPx = (page) =>
	page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function checkPhoneDrawer(browser, baseUrl) {
	const page = await openDocs(browser, baseUrl, pages.introduction, { width: 390, height: 844 });

	// Ctrl+K opens the closed drawer and focuses search once it is shown.
	await page.keyboard.down('Control');
	await page.keyboard.press('k');
	await page.keyboard.up('Control');
	assert.equal(
		await happens(page, () => document.activeElement?.name === 'search'),
		true,
		'Ctrl+K on the closed phone drawer focuses the search field'
	);
	assert.equal(await drawerOpen(page), true, 'Ctrl+K opens the phone drawer');
	assert.equal(
		await page.evaluate(() => document.body.style.overflow),
		'hidden',
		'the page behind the open drawer does not scroll'
	);

	// Escape closes the drawer and returns focus to Menu.
	await page.keyboard.press('Escape');
	assert.equal(await drawerIs(page, false), true, 'Escape closes the drawer');
	assert.equal(await focusOnMenu(page), true, 'Escape returns focus to Menu');
	assert.equal(
		await page.evaluate(() => document.body.style.overflow),
		'',
		'page scroll is restored'
	);

	// The visible Close menu button and the backdrop both close it and return focus to Menu.
	await page.click('.menu-btn');
	assert.equal(await drawerIs(page, true), true, 'Menu opens the drawer');
	await page.click('.close-nav');
	assert.equal(await drawerIs(page, false), true, 'Close menu closes the drawer');
	assert.equal(await focusOnMenu(page), true, 'Close menu returns focus to Menu');

	await page.click('.menu-btn');
	assert.equal(await drawerIs(page, true), true, 'Menu reopens the drawer');
	await page.mouse.click(370, 520); // Right of the 300px drawer, over the backdrop.
	assert.equal(await drawerIs(page, false), true, 'the backdrop closes the drawer');
	assert.equal(await focusOnMenu(page), true, 'the backdrop returns focus to Menu');

	// While open, Tab and Shift+Tab cycle inside the drawer and never reach the page.
	await page.click('.menu-btn');
	assert.equal(await drawerIs(page, true), true, 'Menu reopens the drawer for the focus check');
	for (let i = 0; i < 40; i += 1) {
		await page.keyboard.press('Tab');
		assert.equal(await focusInDrawer(page), true, `Tab ${i + 1} stays inside the drawer`);
	}
	for (let i = 0; i < 5; i += 1) {
		await page.keyboard.down('Shift');
		await page.keyboard.press('Tab');
		await page.keyboard.up('Shift');
		assert.equal(await focusInDrawer(page), true, `Shift+Tab ${i + 1} stays inside the drawer`);
	}

	// A search result opens its page and closes the drawer.
	await page.type('input[name="search"]', 'quickstart');
	await page.waitForSelector('.results a');
	await page.click('.results a');
	assert.equal(
		await happens(page, (path) => location.pathname === path, pages.quickstart),
		true,
		'a search result navigates to its page'
	);
	assert.equal(await drawerIs(page, false), true, 'choosing a result closes the drawer');
	assert.equal(
		await page.$eval('input[name="search"]', (input) => input.value),
		'',
		'search is cleared on the new page'
	);
	await page.close();

	// On load the phone tab row is one scrolling line, and the current section tab is in it.
	const phone = await openDocs(browser, baseUrl, pages.decisions, { width: 390, height: 844 });
	const row = await phone.evaluate(() => {
		const tabs = document.querySelector('.tabs');
		const active = document.querySelector('.tabs a[aria-current="true"]').getBoundingClientRect();
		const box = tabs.getBoundingClientRect();
		return {
			height: box.height,
			scrolls: tabs.scrollWidth > tabs.clientWidth,
			activeVisible: active.left >= box.left - 1 && active.right <= box.right + 1,
			count: tabs.querySelectorAll('a').length
		};
	});
	assert.equal(row.count, 6, 'all six section tabs are present');
	assert.ok(row.height <= 60, 'the section tabs stay on one row');
	assert.equal(row.scrolls, true, 'the section tabs scroll sideways at 390px');
	assert.equal(row.activeVisible, true, 'the current section tab is in view after load');
	for (let index = 0; index < row.count; index += 1) {
		const reachable = await phone.$$eval(
			'.tabs a',
			(links, i) => {
				links[i].scrollIntoView({ inline: 'nearest', block: 'nearest' });
				const box = links[i].getBoundingClientRect();
				const tabs = links[i].parentElement.getBoundingClientRect();
				return box.width > 0 && box.left >= tabs.left - 1 && box.right <= tabs.right + 1;
			},
			index
		);
		assert.equal(reachable, true, `section tab ${index + 1} can be scrolled into view`);
	}

	// Copy Page stays a compact header action with its accessible name.
	const copy = await phone.$eval('.copy-page', (button) => ({
		name: button.textContent.trim(),
		height: button.getBoundingClientRect().height
	}));
	assert.equal(copy.name, 'Copy Page', 'Copy Page keeps its name on the phone');
	assert.ok(copy.height <= 40, 'Copy Page is not a separate large row');
	await phone.close();

	// Touch screens get no keyboard shortcut hint. A mouse-driven phone width keeps it.
	const touch = await openDocs(browser, baseUrl, pages.introduction, {
		width: 390,
		height: 844,
		touch: true
	});
	assert.equal(
		await touch.$eval('.search kbd', (kbd) => getComputedStyle(kbd).display),
		'none',
		'the Ctrl K hint is hidden on a touch phone'
	);
	await touch.close();
	const mouse = await openDocs(browser, baseUrl, pages.introduction, { width: 390, height: 844 });
	assert.notEqual(
		await mouse.$eval('.search kbd', (kbd) => getComputedStyle(kbd).display),
		'none',
		'the Ctrl K hint stays on a pointer device'
	);
	await mouse.close();
}

async function checkDesktop(browser, baseUrl) {
	const page = await openDocs(browser, baseUrl, pages.introduction, { width: 1280, height: 900 });

	// Desktop Ctrl+K only focuses search: no drawer, no scrim.
	await page.keyboard.down('Control');
	await page.keyboard.press('k');
	await page.keyboard.up('Control');
	assert.equal(
		await happens(page, () => document.activeElement?.name === 'search'),
		true,
		'Ctrl+K focuses search on desktop'
	);
	assert.equal(await page.$('.scrim'), null, 'desktop Ctrl+K adds no scrim');
	assert.equal(await drawerOpen(page), false, 'desktop Ctrl+K does not open the drawer');
	assert.equal(await overflowPx(page), 0, 'the 1280px layout has no horizontal overflow');
	assert.notEqual(
		await page.$eval('.toc', (toc) => getComputedStyle(toc).display),
		'none',
		'the On this page rail is shown at 1280px'
	);

	// A drawer left open when the window grows to desktop width closes.
	await page.setViewport({ width: 390, height: 844 });
	await page.click('.menu-btn');
	assert.equal(await drawerIs(page, true), true, 'Menu opens the drawer before the resize');
	await page.setViewport({ width: 1280, height: 900 });
	assert.equal(await drawerIs(page, false), true, 'widening to desktop closes the drawer');
	assert.equal(await page.$('.scrim'), null, 'widening to desktop removes the scrim');
	assert.equal(
		await page.evaluate(() => document.body.style.overflow),
		'',
		'widening to desktop restores page scroll'
	);
	await page.close();
}

async function checkOverflowAndTheme(browser, baseUrl) {
	for (const width of [320, 390, 768]) {
		for (const mode of ['dark', 'light']) {
			for (const path of Object.values(pages)) {
				const page = await openDocs(browser, baseUrl, path, { width, height: 800, mode });
				const applied = await page.evaluate(() => document.documentElement.dataset.landingMode);
				assert.equal(applied, mode, `${path} applies the ${mode} theme`);
				// Chrome reports computed colours as oklch(L C H); L is the first number.
				const paper = await page.$eval('.docs', (docs) => getComputedStyle(docs).backgroundColor);
				const lightness = Number(paper.match(/^oklch\(([\d.]+)/)?.[1]);
				assert.ok(
					mode === 'dark' ? lightness < 0.3 : lightness > 0.9,
					`${path} ${mode} paper is ${mode} (${paper})`
				);
				assert.equal(
					await overflowPx(page),
					0,
					`${path} at ${width}px in ${mode} has no horizontal overflow`
				);
				await page.close();
			}
		}
	}
}

async function checkContrast(browser, baseUrl) {
	for (const mode of ['dark', 'light']) {
		const page = await openDocs(browser, baseUrl, pages.introduction, { mode, width: 1440 });
		const values = await page.evaluate(() => {
			const context = document
				.createElement('canvas')
				.getContext('2d', { willReadFrequently: true });
			function rgba(color) {
				context.clearRect(0, 0, 1, 1);
				context.fillStyle = color;
				context.fillRect(0, 0, 1, 1);
				return [...context.getImageData(0, 0, 1, 1).data].map((v, i) => (i === 3 ? v / 255 : v));
			}
			function background(element) {
				let result = [255, 255, 255];
				const chain = [];
				for (let node = element; node; node = node.parentElement) chain.unshift(node);
				for (const node of chain) {
					const color = rgba(getComputedStyle(node).backgroundColor);
					result = result.map((v, i) => color[i] * color[3] + v * (1 - color[3]));
				}
				return result;
			}
			function luminance(color) {
				return color
					.map((v) => v / 255)
					.map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
					.reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
			}
			const selectors = [
				'.subtitle',
				'.group a',
				'.tabs a',
				'.toc a',
				'.prose p',
				'.prose a',
				'.docs-card-title',
				'.docs-card-text',
				'.callout-label',
				'.copy-page',
				'.pager a'
			];
			return selectors.flatMap((selector) =>
				[...document.querySelectorAll(selector)]
					.filter((el) => el.getClientRects().length)
					.map((element) => {
						const style = getComputedStyle(element);
						const bg = background(element);
						const fg = rgba(style.color);
						const ink = bg.map((v, i) => fg[i] * fg[3] + v * (1 - fg[3]));
						const l = [luminance(ink), luminance(bg)].sort((a, b) => b - a);
						return {
							selector,
							text: element.textContent.trim().slice(0, 60),
							ratio: (l[0] + 0.05) / (l[1] + 0.05)
						};
					})
			);
		});
		for (const value of values)
			assert.ok(
				value.ratio >= 4.5,
				`${mode} ${value.selector} "${value.text}" contrast ${value.ratio.toFixed(2)} is at least 4.5`
			);
		console.log(
			`${mode} docs text minimum contrast: ${Math.min(...values.map((v) => v.ratio)).toFixed(2)}:1`
		);
		await page.close();
	}
}

async function checkCopy(browser, baseUrl) {
	const page = await openDocs(browser, baseUrl, pages.quickstart, { width: 1280, height: 900 });
	// The clipboard boundary is spied so the page's own copy code runs unchanged.
	await page.evaluate(() => {
		window.__copied = [];
		navigator.clipboard.writeText = async (text) => {
			window.__copied.push(text);
		};
	});

	await page.click('.copy-page');
	assert.equal(
		await happens(page, () => document.querySelector('.copy-page').textContent.includes('Copied')),
		true,
		'Copy Page shows Copied'
	);
	const title = await page.$eval('h1', (heading) => heading.textContent.trim());
	const pageText = await page.evaluate(() => window.__copied[0]);
	assert.ok(pageText.startsWith(`${title}\n\n`), 'Copy Page starts with the page title');
	assert.ok(
		pageText.includes('Check these on the machine before you install.'),
		'Copy Page includes the article text'
	);

	assert.ok((await page.$$('.codeblock')).length > 0, 'the Quickstart page has a real code block');
	const expectedCode = await page.$eval('.codeblock code', (code) => code.textContent);
	assert.ok(pageText.includes(expectedCode), 'Copy Page includes the code block text');
	await page.click('.codeblock .copy-code');
	assert.equal(
		await happens(page, () => window.__copied.length === 2),
		true,
		'the code copy button writes to the clipboard'
	);
	assert.equal(
		await page.evaluate(() => window.__copied[1]),
		expectedCode,
		'code copy writes the block text'
	);
	await page.close();
}

let server;
let browser;
try {
	await requireBuiltPages();
	server = await serveBuild();
	const baseUrl = `http://127.0.0.1:${server.address().port}`;
	browser = await puppeteer.launch({
		executablePath: await chromeExecutable(),
		headless: true,
		args: ['--no-sandbox', '--disable-gpu']
	});

	await checkPhoneDrawer(browser, baseUrl);
	await checkDesktop(browser, baseUrl);
	await checkOverflowAndTheme(browser, baseUrl);
	await checkCopy(browser, baseUrl);
	await checkContrast(browser, baseUrl);

	console.log('Docs browser checks passed.');
} finally {
	if (browser) await browser.close();
	if (server)
		await new Promise((resolveClose, reject) =>
			server.close((error) => (error ? reject(error) : resolveClose()))
		);
}
