import assert from 'node:assert/strict';
import { access, readFile, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

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
const pixel = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="#dc2626"/></svg>')}`;
const model = {
	id: 'focus-model',
	name: 'Focus model',
	capabilities: ['realtime', 'text_to_image'],
	min_vram_gb: 0,
	default: true,
	realtime_p95_ms: 100,
	estimated_gpu_ms_default: 100,
	parameters: { type: 'object', required: ['prompt'], properties: { prompt: { type: 'string' } } }
};
const accountA = {
	id: '550e8400-e29b-41d4-a716-446655440041',
	email: 'a@example.com',
	role: 'admin'
};
const accountB = {
	id: '550e8400-e29b-41d4-a716-446655440042',
	email: 'b@example.com',
	role: 'admin'
};

function generation(id, prompt, state = 'queued') {
	return {
		id,
		model_id: model.id,
		source_asset_id: null,
		has_derivatives: false,
		params: { prompt },
		state,
		progress: null,
		gpu_ms: null,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: '2026-10-27T10:00:00.000Z',
		dispatched_at: '2026-10-27T10:00:00.000Z',
		finished_at: null,
		starred_at: null,
		expired: false,
		expired_favorite: false,
		assets: [
			{
				id: `23000000-0000-4000-8000-${id.slice(-12)}`,
				url: pixel,
				thumbnail_url: pixel,
				download_url: pixel,
				width: 8,
				height: 8
			}
		]
	};
}

const rowA = generation('13000000-0000-4000-8000-000000000041', 'A queued image');
const rowB = generation('13000000-0000-4000-8000-000000000042', 'B queued image');
const fixture = {
	account: { status: 200, body: accountA },
	config: { status: 200, body: { auth_methods: [] } },
	row: rowA
};
const requests = [];

function apiReply(path) {
	if (path === '/api/v1/account') return fixture.account;
	if (path === '/api/v1/config') return fixture.config;
	if (path === '/api/v1/models') return { status: 200, body: [model] };
	if (path === '/api/v1/generations/models') return { status: 200, body: { models: [model] } };
	if (path === '/api/v1/generations') {
		return path.includes('starred')
			? { status: 200, body: [] }
			: { status: 200, body: [fixture.row] };
	}
	if (/^\/api\/v1\/generations\/[^/]+\/events$/.test(path)) return { status: 200, body: {} };
	if (/^\/api\/v1\/generations\/[^/]+/.test(path)) return { status: 404, body: {} };
	return { status: 200, body: [] };
}

async function serveBuild() {
	const server = createServer(async (request, response) => {
		const url = new URL(request.url, 'http://localhost');
		if (url.pathname.startsWith('/api/')) {
			const path = `${url.pathname}${url.search}`;
			requests.push(path);
			const answer = apiReply(url.pathname);
			const contentType = answer.contentType ?? 'application/json';
			response.writeHead(answer.status ?? 200, {
				'Content-Type': contentType,
				'Cache-Control': 'no-store'
			});
			response.end(answer.raw ?? JSON.stringify(answer.body));
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

async function newPage(browser, server, { holdInitialAccount = false, path = '/app' } = {}) {
	const page = await browser.newPage();
	page.setDefaultTimeout(timeoutMs);
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.stack ?? error.message));
	await page.evaluateOnNewDocument((shouldHoldAccount) => {
		const nativeNow = Date.now.bind(Date);
		let offset = 0;
		Date.now = () => nativeNow() + offset;
		window.advanceFocusClock = (milliseconds) => (offset += milliseconds);
		Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
		window.__initialHistoryLength = history.length;
		window.__holdNextAccountJson = shouldHoldAccount;
		window.__focusStreams = [];
		window.EventSource = class {
			constructor(url) {
				this.url = String(url);
				this.closed = false;
				window.__focusStreams.push(this);
			}
			close() {
				this.closed = true;
			}
		};
		const nativeFetch = window.fetch.bind(window);
		window.fetch = async (input, init) => {
			const response = await nativeFetch(input, init);
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/account' && window.__holdNextAccountJson) {
				window.__holdNextAccountJson = false;
				const payload = await response.clone().json();
				let release;
				const held = new Promise((resolveHeld) => (release = resolveHeld));
				window.__releaseHeldAccountJson = () => release();
				window.__heldAccountJson = true;
				response.json = () => held.then(() => payload);
			}
			if (url.pathname === '/api/v1/generations' && window.__holdNextHistoryJson) {
				window.__holdNextHistoryJson = false;
				const payload = await response.clone().json();
				let release;
				const held = new Promise((resolveHeld) => (release = resolveHeld));
				window.__releaseHeldHistory = () => release();
				window.__heldHistoryJson = true;
				response.json = () => held.then(() => payload);
			}
			return response;
		};
	}, holdInitialAccount);
	const address = server.address();
	await page.goto(`http://127.0.0.1:${address.port}${path}`, { waitUntil: 'domcontentloaded' });
	return { page, errors };
}

async function waitForThumbnail(page) {
	await page.waitForSelector('[data-strip-thumb]', { timeout: timeoutMs });
	await page.bringToFront();
}

async function dueFocus(page, { duplicate = false } = {}) {
	const before = requests.length;
	await page.evaluate((sendDuplicate) => {
		window.advanceFocusClock(31_000);
		window.dispatchEvent(new Event('focus'));
		if (sendDuplicate) document.dispatchEvent(new Event('visibilitychange'));
	}, duplicate);
	await waitFor(
		() => requests.slice(before).some((path) => path === '/api/v1/account'),
		'account probe'
	);
	await pause(250);
	return requests.slice(before);
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

		fixture.account = { status: 200, body: accountA };
		fixture.row = rowA;
		const heldStartupStart = requests.length;
		const heldStartup = await newPage(browser, server, { holdInitialAccount: true });
		await waitFor(
			() => heldStartup.page.evaluate(() => window.__heldAccountJson === true),
			'held startup account JSON'
		);
		const beforeStartupRelease = requests.slice(heldStartupStart);
		assert.deepEqual(
			beforeStartupRelease,
			['/api/v1/account'],
			'startup waits for account JSON before private model or history requests'
		);
		await heldStartup.page.evaluate(() => window.__releaseHeldAccountJson());
		await waitForThumbnail(heldStartup.page);
		await heldStartup.page.close();

		fixture.account = { status: 401, body: { detail: 'sign in' } };
		const startup401Start = requests.length;
		const startup401 = await newPage(browser, server, {
			path: '/app?view=metrics&tab=benchmarks'
		});
		await startup401.page.waitForFunction(() => location.pathname === '/login', {
			timeout: timeoutMs
		});
		const startup401State = await startup401.page.evaluate(() => ({
			search: location.search,
			historyLength: history.length,
			initialHistoryLength: window.__initialHistoryLength
		}));
		const startup401Requests = requests.slice(startup401Start);
		assert.equal(startup401Requests[0], '/api/v1/account');
		assert.ok(
			!startup401Requests.some(
				(path) => path.startsWith('/api/v1/models') || path.startsWith('/api/v1/generations')
			),
			'startup 401 does not preload private data'
		);
		assert.equal(
			new URLSearchParams(startup401State.search).get('next'),
			'/app?view=metrics&tab=benchmarks',
			'startup 401 redirects with the app view and tab return path'
		);
		assert.equal(
			startup401State.historyLength,
			startup401State.initialHistoryLength,
			'startup 401 replaces the app history entry'
		);
		assert.deepEqual(startup401.errors, []);
		await startup401.page.close();

		fixture.account = { status: 200, body: accountA };
		fixture.config = { status: 200, body: { auth_methods: [] } };
		fixture.row = rowA;
		const first = await newPage(browser, server);
		const page = first.page;
		await waitForThumbnail(page);
		await waitFor(() => page.evaluate(() => window.__focusStreams.length > 0), 'A event stream');
		await page.$eval('[data-strip-thumb]', (element) => {
			element.dataset.focusMarker = 'same-owner';
			window.__focusMarkerNode = element;
		});
		const sameOwner = await dueFocus(page, { duplicate: true });
		const sameOwnerState = await page.evaluate(() => ({
			marker: document.querySelector('[data-strip-thumb]')?.dataset.focusMarker ?? null,
			nodeRetained: document.querySelector('[data-strip-thumb]') === window.__focusMarkerNode,
			streamCount: window.__focusStreams.length
		}));
		assert.equal(sameOwner.filter((path) => path === '/api/v1/account').length, 1);
		assert.equal(sameOwner[0], '/api/v1/account', 'same-owner focus probes before private refresh');
		assert.equal(sameOwnerState.marker, 'same-owner');
		assert.equal(
			sameOwnerState.nodeRetained,
			true,
			'same-owner focus keeps the keyed view mounted'
		);

		await page.evaluate(() => (window.__holdNextHistoryJson = true));
		const oldFocusStart = requests.length;
		const oldFocusPromise = dueFocus(page);
		await waitFor(
			() => page.evaluate(() => window.__heldHistoryJson === true),
			'held A history JSON'
		);
		fixture.account = { status: 200, body: accountB };
		fixture.row = rowB;
		const beforeB = requests.length;
		const newOwner = await dueFocus(page);
		await waitFor(
			() =>
				requests.slice(beforeB).filter((path) => path === '/api/v1/generations?limit=50').length ===
				1,
			'one B history preload'
		);
		await waitFor(
			() =>
				page.evaluate(() =>
					window.__focusStreams.some((stream) => stream.url.includes('000000000042/events'))
				),
			'B event stream'
		);
		await page.evaluate(() => window.__releaseHeldHistory());
		await oldFocusPromise;
		await pause(250);
		const ownerSwitchState = await page.evaluate(() => ({
			titles: [...document.querySelectorAll('[data-strip-thumb]')].map((element) => element.title),
			streams: window.__focusStreams.map((stream) => ({ url: stream.url, closed: stream.closed }))
		}));
		assert.equal(newOwner[0], '/api/v1/account', 'the new owner is checked before its preload');
		assert.equal(
			requests.slice(beforeB).filter((path) => path === '/api/v1/generations?limit=50').length,
			1,
			'owner change starts one history preload'
		);
		assert.deepEqual(
			ownerSwitchState.titles,
			['B queued image'],
			'late A history cannot replace B rows'
		);
		assert.ok(
			ownerSwitchState.streams.some(
				(stream) => stream.url.includes('000000000041/events') && stream.closed
			)
		);
		assert.ok(
			ownerSwitchState.streams.some(
				(stream) => stream.url.includes('000000000042/events') && !stream.closed
			)
		);
		assert.ok(requests.slice(oldFocusStart).includes('/api/v1/account'));

		const bNode = await page.$('[data-strip-thumb]');
		await bNode.evaluate((element) => {
			element.dataset.focusMarker = 'B warm';
			window.__focusMarkerNode = element;
		});
		fixture.account = { status: 503, body: { detail: 'unavailable' } };
		const unavailable = await dueFocus(page);
		assert.deepEqual(
			unavailable,
			['/api/v1/account'],
			'503 keeps warm state and skips private refresh'
		);
		assert.equal(
			await page.$eval('[data-strip-thumb]', (element) => element === window.__focusMarkerNode),
			true
		);

		fixture.account = {
			status: 200,
			raw: '<html>temporary proxy page</html>',
			contentType: 'text/html'
		};
		const unreadable = await dueFocus(page);
		assert.deepEqual(
			unreadable,
			['/api/v1/account'],
			'unreadable 200 keeps warm state and skips refresh'
		);
		fixture.account = { status: 200, body: accountB };
		const retried = await dueFocus(page);
		assert.equal(retried[0], '/api/v1/account', 'a later due focus retries the probe');
		assert.ok(retried.some((path) => path === '/api/v1/models'));

		fixture.account = { status: 401, body: { detail: 'sign in' } };
		const signedOut = await dueFocus(page);
		await page.waitForFunction(() => location.pathname === '/login', { timeout: timeoutMs });
		assert.equal(signedOut[0], '/api/v1/account', 'focus checks identity before redirecting');
		assert.ok(
			!signedOut.some(
				(path) => path.startsWith('/api/v1/models') || path.startsWith('/api/v1/generations')
			),
			'focus 401 does not refresh private data'
		);
		assert.deepEqual(first.errors, []);
		await page.close();

		fixture.account = {
			status: 200,
			body: { id: 'invalid-id', email: 'known-role@example.com', role: 'user' }
		};
		fixture.row = rowA;
		const invalidStart = requests.length;
		const invalidOwner = await newPage(browser, server);
		await invalidOwner.page.waitForSelector('[data-studio-shell]', { timeout: timeoutMs });
		await pause(250);
		const invalidRequests = requests.slice(invalidStart);
		assert.ok(
			invalidRequests.includes('/api/v1/account'),
			'invalid UUID startup checks the account'
		);
		assert.ok(
			!invalidRequests.some((path) => path.startsWith('/api/v1/generations')),
			'invalid UUID with valid role sends no history request'
		);
		assert.match(
			await invalidOwner.page.$eval('body', (element) => element.innerText),
			/known-role@example\.com/
		);
		assert.deepEqual(invalidOwner.errors, []);
		await invalidOwner.page.close();

		fixture.account = { status: 404, body: { detail: 'local mode' } };
		fixture.config = { status: 200, body: { auth_methods: [] } };
		fixture.row = rowA;
		const local = await newPage(browser, server);
		await waitForThumbnail(local.page);
		const localRefresh = await dueFocus(local.page);
		assert.equal(localRefresh[0], '/api/v1/account');
		assert.equal(localRefresh[1], '/api/v1/config');
		assert.ok(
			localRefresh.includes('/api/v1/models'),
			'confirmed local mode refreshes private views'
		);

		fixture.config = { status: 503, body: { detail: 'unavailable' } };
		const configFailure = await dueFocus(local.page);
		assert.deepEqual(configFailure, ['/api/v1/account', '/api/v1/config']);
		fixture.config = { status: 200, body: { auth_methods: ['password'] } };
		const configuredAuth = await dueFocus(local.page);
		assert.deepEqual(configuredAuth, ['/api/v1/account', '/api/v1/config']);
		assert.deepEqual(local.errors, []);
		await local.page.close();

		console.log(
			'owner-focus browser checks passed: same-owner order, duplicate events, A-to-B late history and stream fencing, failed probe retry, 401, invalid UUID, and local-mode outcomes'
		);
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
}

await main();
