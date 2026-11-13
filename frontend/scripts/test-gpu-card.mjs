import assert from 'node:assert/strict';
import { access, readFile, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

// The first-run "Where are your GPUs?" card on the product build: who sees it,
// what each option does, the keyboard path, the failed list, the one-time flag,
// and that the guided tour waits until the card is finished.
const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
const timeoutMs = 15_000;
const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));
const contentTypes = {
	'.html': 'text/html',
	'.js': 'application/javascript',
	'.css': 'text/css',
	'.json': 'application/json',
	'.svg': 'image/svg+xml',
	'.woff2': 'font/woff2'
};
const cardKey = 'potocolom-gpu-card-done';
const model = {
	id: 'gpu-card-model',
	name: 'Card model',
	capabilities: ['text_to_image'],
	min_vram_gb: 0,
	default: true,
	realtime_p95_ms: 100,
	estimated_gpu_ms_default: 100,
	parameters: { type: 'object', required: ['prompt'], properties: { prompt: { type: 'string' } } }
};
const english = JSON.parse(
	await readFile(new URL('../src/lib/i18n/en.json', import.meta.url), 'utf8')
);
const tourTitle = english['app.tour.canvas_title'];
const unreadable = english['app.gpu_card.unreadable'];

// Scenario state, changed between scenarios.
let accountMode = 'admin';
let workers = [];
let workersFail = false;
let hits = new Map();

function count(path) {
	return hits.get(path) ?? 0;
}

function apiReply(pathname) {
	if (pathname === '/api/v1/account')
		return accountMode === 'local'
			? { status: 404, body: {} }
			: {
					status: 200,
					body: {
						id: '550e8400-e29b-41d4-a716-446655440061',
						email: 'card@example.com',
						role: accountMode
					}
				};
	if (pathname === '/api/v1/config') return { status: 200, body: { auth_methods: [] } };
	if (pathname === '/api/v1/models') return { status: 200, body: [model] };
	if (pathname === '/api/v1/generations/models') return { status: 200, body: { models: [model] } };
	if (pathname === '/api/v1/admin/models') {
		if (workersFail) return { status: 500, body: { detail: 'boom' } };
		return { status: 200, body: workers };
	}
	if (pathname === '/api/v1/admin/fleet/members') return { status: 200, body: [] };
	if (pathname === '/api/v1/metrics/gpu/history')
		return { status: 200, body: { from: '', to: '', rollup: 'raw', samples: [] } };
	return { status: 200, body: [] };
}

async function serveBuild() {
	const server = createServer(async (request, response) => {
		const url = new URL(request.url, 'http://localhost');
		if (url.pathname.startsWith('/api/')) {
			hits.set(url.pathname, count(url.pathname) + 1);
			const answer = apiReply(url.pathname);
			response.writeHead(answer.status, {
				'Content-Type': 'application/json',
				'Cache-Control': 'no-store'
			});
			response.end(JSON.stringify(answer.body));
			return;
		}
		const file = resolve(build, `.${decodeURIComponent(url.pathname)}`);
		if (file !== build && !file.startsWith(`${build}/`)) {
			response.writeHead(404).end();
			return;
		}
		const candidates = [file, `${file}.html`, join(file, 'index.html'), join(build, 'index.html')];
		let selected;
		for (const candidate of candidates) {
			if (
				await stat(candidate).then(
					(value) => value.isFile(),
					() => false
				)
			) {
				selected = candidate;
				break;
			}
		}
		if (!selected) {
			response.writeHead(404).end();
			return;
		}
		response.writeHead(200, {
			'Content-Type': contentTypes[extname(selected)] ?? 'application/octet-stream'
		});
		response.end(await readFile(selected));
	});
	await new Promise((resolveListen) => server.listen(0, '127.0.0.1', resolveListen));
	return server;
}

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
	throw new Error('No Chrome executable found. Set PUPPETEER_EXECUTABLE_PATH or CHROME_PATH.');
}

async function waitFor(predicate, description) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await predicate()) return;
		await pause(25);
	}
	throw new Error(`Timed out waiting for ${description}`);
}

async function openStudio(context, server, { blockStorage = false, path = '/app' } = {}) {
	hits = new Map();
	const page = await context.newPage();
	page.setDefaultTimeout(timeoutMs);
	await page.setViewport({ width: 1280, height: 900 });
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.evaluateOnNewDocument((blocked) => {
		// The studio opens streams it never reads here; the card does not use them.
		window.EventSource = class {
			addEventListener() {}
			removeEventListener() {}
			close() {}
		};
		if (blocked) {
			Object.defineProperty(window, 'localStorage', {
				configurable: true,
				get() {
					throw new Error('SecurityError');
				}
			});
		}
	}, blockStorage);
	await page.goto(`http://127.0.0.1:${server.address().port}${path}`, {
		waitUntil: 'domcontentloaded'
	});
	return { page, errors };
}

const card = (page) => page.$('[data-testid="gpu-card"]');
const cardOpen = async (page) => (await card(page)) !== null;
const tourOpen = async (page) => (await page.$('[data-studio-tour]')) !== null;
const tourHeading = (page) =>
	page.evaluate(() => document.querySelector('[data-studio-tour] h2')?.textContent.trim() ?? null);
const checked = (page, testid) => page.$eval(`[data-testid="${testid}"]`, (input) => input.checked);
const storedFlag = (page, key) =>
	page.evaluate((name) => {
		try {
			return localStorage.getItem(name);
		} catch {
			return 'blocked';
		}
	}, key);

// The studio asks for the models before the tour may start. Waiting for that
// request, then a settle pause, makes "no tour" a real claim rather than a race.
async function settleAfterModels(page) {
	await waitFor(() => count('/api/v1/models') > 0, 'the models request');
	await pause(400);
}

// Moves focus with Tab until the element with `testid` has it.
async function tabTo(page, testid) {
	for (let press = 0; press < 12; press++) {
		const focused = await page.evaluate(
			() => document.activeElement?.getAttribute('data-testid') ?? null
		);
		if (focused === testid) return;
		await page.keyboard.press('Tab');
	}
	throw new Error(`Tab never reached ${testid}`);
}

async function scenarioAdminWithWorker(browser, server) {
	accountMode = 'admin';
	workers = [{ worker_id: 'm-a1' }];
	workersFail = false;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	assert.equal(await checked(page, 'gpu-card-this'), true, '"This computer" is the default');
	await page.waitForSelector('[data-testid="gpu-card-worker"]');
	assert.equal(
		await page.$eval('[data-testid="gpu-card-worker"]', (item) => item.textContent.trim()),
		'm-a1',
		'the connected worker is listed'
	);

	// Focus stays inside the dialog while the user tabs.
	for (let press = 0; press < 10; press++) {
		await page.keyboard.press('Tab');
		const inside = await page.evaluate(
			() => document.activeElement?.closest('[role="dialog"]') !== null
		);
		assert.equal(inside, true, 'focus stays in the card');
	}

	await settleAfterModels(page);
	assert.equal(await tourOpen(page), false, 'the tour waits behind the card');

	await page.click('[data-testid="gpu-card-continue"]');
	await page.waitForSelector('[data-studio-tour] h2', { hidden: false });
	await waitFor(async () => (await tourHeading(page)) === tourTitle, 'the tour after the card');
	await waitFor(async () => !(await cardOpen(page)), 'the card closes on Continue');
	assert.equal(await storedFlag(page, cardKey), '1', 'the card flag is written');

	await page.keyboard.press('Escape');
	await waitFor(async () => !(await tourOpen(page)), 'the tour to close');
	hits = new Map();
	await page.reload({ waitUntil: 'domcontentloaded' });
	await settleAfterModels(page);
	assert.equal(await cardOpen(page), false, 'a second visit shows no card');
	assert.equal(await tourOpen(page), false, 'a finished tour stays shut');
	assert.deepEqual(errors, []);
	await context.close();
}

async function scenarioAdminNoWorkerKeyboard(browser, server) {
	accountMode = 'admin';
	workers = [];
	workersFail = false;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	assert.equal(await checked(page, 'gpu-card-other'), true, '"Other computers" is the default');
	await settleAfterModels(page);
	assert.equal(await tourOpen(page), false, 'the tour waits behind the card');

	// Keyboard only: arrows choose the option, Tab reaches Continue, Enter submits.
	// Up first, so the check sees the selection move away from the default.
	await page.focus('[data-testid="gpu-card-other"]');
	await page.keyboard.press('ArrowUp');
	assert.equal(await checked(page, 'gpu-card-this'), true, 'ArrowUp selects the option above');
	await page.keyboard.press('ArrowDown');
	assert.equal(await checked(page, 'gpu-card-other'), true, 'ArrowDown selects the next option');
	await tabTo(page, 'gpu-card-continue');
	await page.keyboard.press('Enter');

	await waitFor(() => page.url().includes('tab=fleet'), 'the fleet tab');
	await page.waitForSelector('[data-testid="admin-fleet-name"]');
	assert.match(
		await page.evaluate(() => document.querySelector('[role="dialog"]')?.textContent ?? ''),
		/Add a GPU/,
		'the Add a GPU dialog is open'
	);
	await pause(400);
	assert.equal(await tourOpen(page), false, 'the tour does not start over the Administration view');
	await waitFor(async () => !(await cardOpen(page)), 'the card is gone');
	assert.equal(await storedFlag(page, cardKey), '1', 'the card flag is written');

	// A later visit to Generate starts the tour, because the tour flag is still unset.
	await page.goto(`http://127.0.0.1:${server.address().port}/app`, {
		waitUntil: 'domcontentloaded'
	});
	await waitFor(async () => (await tourHeading(page)) === tourTitle, 'the tour on a later visit');
	assert.equal(await cardOpen(page), false, 'the card does not come back');
	assert.deepEqual(errors, []);
	await context.close();
}

async function scenarioEscapeDismiss(browser, server) {
	accountMode = 'admin';
	workers = [{ worker_id: 'm-a1' }];
	workersFail = false;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await settleAfterModels(page);
	await page.keyboard.press('Escape');
	await waitFor(async () => !(await cardOpen(page)), 'the card to close on Escape');
	assert.equal(await storedFlag(page, cardKey), '1', 'a dismissed card is marked done');
	await waitFor(
		async () => (await tourHeading(page)) === tourTitle,
		'the tour after a dismissed card'
	);
	assert.deepEqual(errors, []);
	await context.close();
}

async function scenarioFailedList(browser, server) {
	accountMode = 'admin';
	workers = [];
	workersFail = true;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await page.waitForSelector('[data-testid="gpu-card-workers"] [role="alert"]');
	assert.equal(await checked(page, 'gpu-card-this'), true, 'a failed list selects "This computer"');
	assert.equal(
		await page.$eval('[data-testid="gpu-card-workers"] [role="alert"]', (alert) =>
			alert.textContent.trim()
		),
		unreadable,
		'the card says the GPUs could not be read'
	);
	assert.equal(await page.$('[data-testid="gpu-card-worker"]'), null, 'no list is shown');
	workersFail = false;
	assert.deepEqual(errors, []);
	await context.close();
}

async function scenarioViewer(browser, server) {
	accountMode = 'viewer';
	workers = [];
	workersFail = false;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await waitFor(() => count('/api/v1/account') > 0, 'the account probe');
	await pause(400);
	assert.equal(await cardOpen(page), false, 'a viewer never sees the card');
	assert.equal(await storedFlag(page, cardKey), null, 'a viewer does not write the card flag');
	assert.deepEqual(errors, []);
	await context.close();
	accountMode = 'admin';
}

async function scenarioNoneMode(browser, server) {
	accountMode = 'local';
	workers = [{ worker_id: 'm-a1' }];
	workersFail = false;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	assert.equal(
		await checked(page, 'gpu-card-this'),
		true,
		'the implicit administrator gets the card'
	);
	await page.click('[data-testid="gpu-card-continue"]');
	await waitFor(() => cardOpen(page).then((open) => !open), 'the card to close');
	assert.deepEqual(errors, []);
	await context.close();
	accountMode = 'admin';
}

async function scenarioBlockedStorage(browser, server) {
	accountMode = 'admin';
	workers = [];
	workersFail = false;
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server, { blockStorage: true });
	await settleAfterModels(page);
	assert.equal(await cardOpen(page), false, 'blocked storage never shows the card');
	assert.equal(await tourOpen(page), false, 'blocked storage never auto-starts the tour');
	assert.deepEqual(errors, []);
	await context.close();
}

async function main() {
	const server = await serveBuild();
	let browser;
	try {
		browser = await puppeteer.launch({
			headless: true,
			executablePath: await chromeExecutable(),
			args: ['--no-sandbox', '--disable-dev-shm-usage']
		});
		await scenarioAdminWithWorker(browser, server);
		await scenarioAdminNoWorkerKeyboard(browser, server);
		await scenarioEscapeDismiss(browser, server);
		await scenarioFailedList(browser, server);
		await scenarioViewer(browser, server);
		await scenarioNoneMode(browser, server);
		await scenarioBlockedStorage(browser, server);
		console.log(
			'GPU card browser checks passed: default option, worker list, focus, both endings, dismissal, failed list, viewer, none mode, blocked storage and the tour order'
		);
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
}

await main();
