import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChrome, serveBuild } from './browser-kit.mjs';

// The first-run setup card in the studio on the product build, steps 2 to 4: who
// sees it, the GPU step, the model step and its installs, the ready step, the
// keyboard path, the failed list, the one-time flag, the tour order, and the user
// menu entry that opens the card again.
const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
const timeoutMs = 15_000;
const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));
const GIB = 1024 ** 3;
const cardKey = 'potocolom-gpu-card-done';
const tourKey = 'potocolom-tour-done';
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
const readyTitle = english['app.gpu_card.ready_title'];
const downloadingTitle = english['app.gpu_card.downloading_title'];
const tokenText = english['app.gpu_card.failed_token'];

// The models the mock workers offer, shaped like GET /api/v1/admin/models.
const catalog = {
	sdxl: {
		id: 'sdxl',
		name: 'SDXL',
		capabilities: ['text_to_image'],
		download_bytes: 7 * GIB,
		min_vram_gb: 8,
		tier: 'standard',
		fits: true
	},
	fast: {
		id: 'fast',
		name: 'SDXL Fast',
		capabilities: ['realtime'],
		download_bytes: 6 * GIB,
		min_vram_gb: 8,
		tier: 'standard',
		fits: true
	},
	esrgan: {
		id: 'esrgan',
		name: 'Real-ESRGAN',
		capabilities: ['upscale'],
		download_bytes: GIB / 4,
		min_vram_gb: 2,
		tier: 'draft',
		fits: true
	}
};
const hardware = {
	gpu_name: 'Mock GPU',
	vram_total_bytes: 16 * GIB,
	disk_total_bytes: 500 * GIB,
	disk_free_bytes: 200 * GIB
};

// One worker as the admin models list answers it. `only` picks the offered models,
// `installed` and `gated` pick models by id.
function worker({
	manageable = true,
	suggested = {},
	installed = [],
	gated = [],
	only = Object.keys(catalog)
} = {}) {
	return {
		worker_id: 'm-a1',
		device: 'rocm',
		manageable,
		reason: manageable ? null : 'Update this worker to manage models here',
		hardware,
		disk_free_bytes: 200 * GIB,
		suggested,
		models: manageable
			? only.map((id) => ({
					...catalog[id],
					installed: installed.includes(id),
					needs_token: gated.includes(id),
					suggested: Object.values(suggested).includes(id),
					download: null
				}))
			: []
	};
}

// The scenario's workers, and the installs the mock has seen since the scenario began.
let accountMode = 'admin';
let workers = [];
let workersFail = false;
let hits = new Map();
let installs = freshInstalls();

// Continue is disabled while the worker list loads; a click before then does nothing.
async function clickContinue(page) {
	await page.waitForSelector('[data-testid="gpu-card-continue"]:not(:disabled)');
	await page.click('[data-testid="gpu-card-continue"]');
}

function freshInstalls() {
	// attempts: every install request, refused or not. posts: the accepted ones.
	return { downloading: new Set(), released: false, posts: [], attempts: [] };
}

function count(path) {
	return hits.get(path) ?? 0;
}

// A model that a POST started is downloading until the test releases it, then installed.
function modelsReply() {
	return workers.map((template) => ({
		...template,
		models: template.models.map((entry) => {
			const started = installs.downloading.has(entry.id);
			const done = started && installs.released;
			return {
				...entry,
				installed: entry.installed || done,
				download:
					started && !done
						? {
								kind: 'install',
								state: 'downloading',
								bytes_done: entry.download_bytes / 2,
								bytes_total: entry.download_bytes,
								error: null,
								op_id: '1'
							}
						: null
			};
		})
	}));
}

function installReply(id) {
	const template = workers[0];
	const entry = template?.models.find((item) => item.id === id);
	installs.attempts.push(id);
	if (!entry) return { status: 404, body: { detail: 'unknown model' } };
	if (entry.needs_token) return { status: 409, body: { detail: 'needs_token' } };
	installs.posts.push(id);
	installs.downloading.add(id);
	return { status: 202, body: { op_id: '1' } };
}

function apiReply(pathname, method) {
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
		return { status: 200, body: modelsReply() };
	}
	const install = /^\/api\/v1\/admin\/workers\/[^/]+\/models\/([^/]+)\/install$/.exec(pathname);
	if (install && method === 'POST') return installReply(decodeURIComponent(install[1]));
	if (pathname === '/api/v1/admin/fleet/members') return { status: 200, body: [] };
	if (pathname === '/api/v1/metrics/gpu/history')
		return { status: 200, body: { from: '', to: '', rollup: 'raw', samples: [] } };
	return { status: 200, body: [] };
}

async function waitFor(predicate, description) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await predicate()) return;
		await pause(25);
	}
	throw new Error(`Timed out waiting for ${description}`);
}

// The studio's API answers from apiReply; the rest is the build, with the app shell as fallback.
function serveStudioBuild() {
	return serveBuild(build, {
		fallback: 'index.html',
		api: (request, response) => {
			const url = new URL(request.url, 'http://localhost');
			if (!url.pathname.startsWith('/api/')) return false;
			hits.set(url.pathname, count(url.pathname) + 1);
			const answer = apiReply(url.pathname, request.method);
			response.writeHead(answer.status, {
				'Content-Type': 'application/json',
				'Cache-Control': 'no-store'
			});
			response.end(JSON.stringify(answer.body));
			return true;
		}
	});
}

async function openStudio(context, server, { blockStorage = false, path = '/app' } = {}) {
	hits = new Map();
	const page = await context.newPage();
	page.setDefaultTimeout(timeoutMs);
	await page.setViewport({ width: 1280, height: 900 });
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.evaluateOnNewDocument((blocked) => {
		// A clock the test can move, so the studio's focus refresh is due at once.
		const realNow = Date.now.bind(Date);
		window.__clockOffset = 0;
		Date.now = () => realNow() + window.__clockOffset;
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
const cardText = (page) =>
	page.evaluate(() => document.querySelector('[data-testid="gpu-card"]')?.textContent ?? '');
const stepNumber = (page) =>
	page.evaluate(
		() => document.querySelector('[data-testid="first-run-frame"]')?.dataset.step ?? null
	);
const waitForStep = (page, number) =>
	waitFor(async () => (await stepNumber(page)) === String(number), `step ${number}`);
const rowText = (page, id) =>
	page.evaluate((model) => {
		const row = document.querySelector(`[data-testid="gpu-card-row"][data-model="${model}"]`);
		return row ? row.textContent.replace(/\s+/g, ' ').trim() : '';
	}, id);

// The studio asks for the models before the tour may start. Waiting for that
// request, then a settle pause, makes "no tour" a real claim rather than a race.
// Two animation frames: every effect queued by the last update has run.
const frames = (page) =>
	page.evaluate(
		() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))
	);

// The studio has answered its models: the panel drops its loading status in the
// same update that runs the tour's first-visit check.
async function settleAfterModels(page) {
	await waitFor(() => count('/api/v1/models') > 0, 'the models request');
	await page.waitForFunction(
		() =>
			document.querySelector('h1.sr-only') &&
			!document.body.textContent.includes('Loading models...')
	);
	await frames(page);
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

// Show tour and Finish setup live in the sidebar's user menu, the last menu button in the footer.
async function openUserMenu(page) {
	await page.waitForSelector('[data-sidebar="footer"] [data-sidebar="menu-button"]');
	const triggers = await page.$$('[data-sidebar="footer"] [data-sidebar="menu-button"]');
	await triggers.at(-1).click();
	await page.waitForSelector('[role="menu"]');
}

async function clickMenuItem(page, label) {
	await page.evaluate((text) => {
		const item = [...document.querySelectorAll('[role="menuitem"]')].find(
			(element) => element.textContent.trim() === text
		);
		if (!item) throw new Error(`No "${text}" menu item`);
		item.click();
	}, label);
}

async function scenarioAdminWithWorker(browser, server) {
	accountMode = 'admin';
	workers = [worker({ suggested: { text_to_image: 'sdxl' }, installed: ['sdxl'] })];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	assert.equal(await checked(page, 'gpu-card-this'), true, '"This computer" is the default');
	await page.waitForSelector('[data-testid="gpu-card-worker"]');
	assert.match(
		await page.$eval('[data-testid="gpu-card-worker"]', (item) => item.textContent),
		/Mock GPU.*16\.00 GiB.*ROCm/s,
		'the connected worker shows its GPU, memory and device'
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

	// The only model is installed, so step 3 is passed over and Continue is step 4.
	await clickContinue(page);
	await waitForStep(page, 4);
	await waitFor(async () => (await cardText(page)).includes(readyTitle), 'the ready step');
	await page.click('[data-testid="gpu-card-open"]');
	await page.waitForSelector('[data-studio-tour] h2', { hidden: false });
	await waitFor(async () => (await tourHeading(page)) === tourTitle, 'the tour after the card');
	await waitFor(async () => !(await cardOpen(page)), 'the card closes on Open the studio');
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
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	// The card opens on "this" while the list loads; the empty list moves it.
	await page.waitForFunction(
		() => document.querySelector('[data-testid="gpu-card-other"]')?.checked === true
	);
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
	await frames(page);
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
	workers = [worker({ suggested: { text_to_image: 'sdxl' }, installed: ['sdxl'] })];
	workersFail = false;
	installs = freshInstalls();
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
	installs = freshInstalls();
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

async function scenarioAccessLostWhileOpen(browser, server) {
	accountMode = 'admin';
	workers = [worker({ suggested: { text_to_image: 'sdxl' }, installed: ['sdxl'] })];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	// The role changes while the tab is open; the focus refresh reads it again.
	accountMode = 'viewer';
	await page.evaluate(() => {
		window.__clockOffset = 24 * 3_600_000;
		window.dispatchEvent(new Event('focus'));
	});
	await waitFor(async () => !(await cardOpen(page)), 'the card to close for a viewer');
	assert.equal(await storedFlag(page, cardKey), null, 'losing access does not mark the card done');
	assert.deepEqual(errors, []);
	await context.close();
	accountMode = 'admin';
}

async function scenarioViewer(browser, server) {
	accountMode = 'viewer';
	workers = [];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await waitFor(() => count('/api/v1/account') > 0, 'the account probe');
	// The card mounts with the studio shell, once the account check has passed.
	await page.waitForSelector('[data-slot="sidebar-container"]', { visible: true });
	await frames(page);
	assert.equal(await cardOpen(page), false, 'a viewer never sees the card');
	assert.equal(await storedFlag(page, cardKey), null, 'a viewer does not write the card flag');
	assert.deepEqual(errors, []);
	await context.close();
	accountMode = 'admin';
}

async function scenarioNoneMode(browser, server) {
	accountMode = 'local';
	workers = [worker({ suggested: { text_to_image: 'sdxl' }, installed: ['sdxl'] })];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	assert.equal(
		await checked(page, 'gpu-card-this'),
		true,
		'the implicit administrator gets the card'
	);
	await settleAfterModels(page);
	await clickContinue(page);
	await waitForStep(page, 4);
	assert.match(await cardText(page), /This computer, no sign in/, 'the account row is local');
	await page.click('[data-testid="gpu-card-open"]');
	await waitFor(() => cardOpen(page).then((open) => !open), 'the card to close');
	assert.deepEqual(errors, []);
	await context.close();
	accountMode = 'admin';
}

async function scenarioBlockedStorage(browser, server) {
	accountMode = 'admin';
	workers = [];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server, { blockStorage: true });
	await settleAfterModels(page);
	assert.equal(await cardOpen(page), false, 'blocked storage never shows the card');
	assert.equal(await tourOpen(page), false, 'blocked storage never auto-starts the tour');
	assert.deepEqual(errors, []);
	await context.close();
}

// Step 3 lists the suggested models with their sizes; Download and continue posts one
// install per ticked model; step 4 shows the progress, then Ready after the poll.
async function scenarioModelDownloads(browser, server) {
	accountMode = 'admin';
	workers = [worker({ suggested: { text_to_image: 'sdxl', realtime: 'fast', upscale: 'esrgan' } })];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await settleAfterModels(page);
	await clickContinue(page);
	await waitForStep(page, 3);

	const boxes = await page.$$eval('[data-testid="gpu-card-model"]', (inputs) =>
		inputs.map((input) => ({
			id: input.dataset.model,
			checked: input.checked,
			disabled: input.disabled
		}))
	);
	assert.deepEqual(
		boxes,
		[
			{ id: 'sdxl', checked: true, disabled: false },
			{ id: 'fast', checked: true, disabled: false },
			{ id: 'esrgan', checked: true, disabled: false }
		],
		'every suggested model is listed and ticked'
	);
	const listed = await cardText(page);
	for (const size of ['7.00 GiB', '6.00 GiB', '0.25 GiB']) {
		assert.ok(listed.includes(size), `${size} is shown on its model`);
	}
	assert.ok(listed.includes('13.25 GiB to download'), 'the total of the ticked sizes is shown');
	assert.ok(listed.includes('200.00 GiB free on this disk'), 'the free disk space is shown');

	await page.click('input[data-model="esrgan"]');
	assert.equal(
		await page.$eval('input[data-model="esrgan"]', (input) => input.checked),
		false,
		'an unticked model stays unticked'
	);
	await page.click('[data-testid="gpu-card-download"]');
	await waitForStep(page, 4);
	assert.deepEqual(installs.posts, ['sdxl', 'fast'], 'one install per ticked model, in order');

	await waitFor(
		async () =>
			(await page.$('[data-testid="gpu-card-row"][data-model="sdxl"] [role="progressbar"]')) !==
			null,
		'a progress bar while the model downloads'
	);
	assert.ok((await cardText(page)).includes(downloadingTitle), 'the models are still downloading');
	assert.equal(
		await page.$('[data-testid="gpu-card-row"][data-model="esrgan"]'),
		null,
		'an unticked model has no row'
	);

	const catalogReads = count('/api/v1/models');
	installs.released = true;
	await waitFor(
		async () =>
			(await rowText(page, 'sdxl')).includes('Ready') &&
			(await rowText(page, 'fast')).includes('Ready'),
		'Ready on both rows after the poll'
	);
	await waitFor(
		async () => count('/api/v1/models') > catalogReads,
		'the studio model list read again once a model is installed'
	);
	await waitFor(async () => (await cardText(page)).includes(readyTitle), 'the ready title');
	assert.deepEqual(errors, []);
	await context.close();
}

// A worker that cannot manage models passes step 3 over.
async function scenarioUnmanageableWorker(browser, server) {
	accountMode = 'admin';
	workers = [worker({ manageable: false })];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await settleAfterModels(page);
	await clickContinue(page);
	await waitForStep(page, 4);
	assert.equal(await page.$('[data-testid="gpu-card-model"]'), null, 'no model step is shown');
	assert.ok((await cardText(page)).includes('Mock GPU'), 'the GPU row names the worker');
	assert.ok(
		(await cardText(page)).includes(english['app.gpu_card.no_model_title']),
		'with nothing installed or downloading, step 4 sends the user to the Models screen'
	);
	assert.deepEqual(installs.posts, [], 'nothing is installed');
	assert.deepEqual(errors, []);
	await context.close();
}

// A token-gated model answers 409; the card stays on step 3 and names the model.
async function scenarioTokenGated(browser, server) {
	accountMode = 'admin';
	// Three picks in use order: the first is accepted, the second refused, the third
	// must never be asked for, because the loop stops at the first refusal.
	workers = [
		worker({
			suggested: { text_to_image: 'sdxl', realtime: 'fast', upscale: 'esrgan' },
			only: ['sdxl', 'fast', 'esrgan'],
			gated: ['fast']
		})
	];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await settleAfterModels(page);
	await clickContinue(page);
	await waitForStep(page, 3);
	await page.click('[data-testid="gpu-card-download"]');
	await waitFor(
		async () => (await page.$('[data-testid="gpu-card"] [role="alert"]')) !== null,
		'the install alert'
	);
	const alert = await page.$eval('[data-testid="gpu-card"] [role="alert"]', (node) =>
		node.textContent.trim()
	);
	assert.ok(alert.includes('SDXL Fast'), 'the alert names the refused model');
	assert.ok(alert.includes(tokenText), 'the alert gives the token reason');
	assert.equal(await stepNumber(page), '3', 'the card stays on step 3');
	assert.deepEqual(installs.posts, ['sdxl'], 'the install before the refusal still started');
	assert.deepEqual(installs.attempts, ['sdxl', 'fast'], 'nothing after the refusal was asked for');
	assert.deepEqual(errors, []);
	await context.close();
}

// The tour switch off sets the tour flag, and the tour does not start. Finish setup
// in the user menu then opens the card again at step 2.
async function scenarioTourSwitchAndReopen(browser, server) {
	accountMode = 'admin';
	workers = [worker({ suggested: { text_to_image: 'sdxl' }, installed: ['sdxl'] })];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await settleAfterModels(page);
	await clickContinue(page);
	await waitForStep(page, 4);
	await page.click('[data-testid="gpu-card-tour"]');
	assert.equal(await checked(page, 'gpu-card-tour'), false, 'the switch is off');
	await page.click('[data-testid="gpu-card-open"]');
	await waitFor(async () => !(await cardOpen(page)), 'the card to close');
	await frames(page);
	assert.equal(await tourOpen(page), false, 'the tour does not start');
	assert.equal(await storedFlag(page, tourKey), '1', 'the tour flag is written');

	await openUserMenu(page);
	await clickMenuItem(page, english['app.gpu_card.reopen']);
	await waitFor(() => cardOpen(page), 'the card to reopen');
	await waitForStep(page, 2);
	assert.equal(await checked(page, 'gpu-card-this'), true, 'the card reopens on step 2');
	await clickContinue(page);
	await waitForStep(page, 4);
	assert.equal(
		await page.$('[data-testid="gpu-card-tour"]'),
		null,
		'a reopened card offers no tour switch: the tour would not start after it'
	);
	await page.click('[data-testid="gpu-card-later"]');
	await waitFor(async () => !(await cardOpen(page)), 'the card to close again');
	await frames(page);
	assert.equal(await tourOpen(page), false, 'the tour stays shut');
	await waitFor(
		() =>
			page.evaluate(() => document.activeElement?.hasAttribute('data-user-menu-trigger') ?? false),
		'focus back on the user menu that opened the card'
	);
	assert.deepEqual(errors, []);
	await context.close();
}

// "Start drawing" appears only when a realtime model is installed.
async function scenarioStartDrawing(browser, server, { realtime }) {
	accountMode = 'admin';
	workers = [
		realtime
			? worker({ suggested: { realtime: 'fast' }, installed: ['fast'] })
			: worker({ suggested: { text_to_image: 'sdxl' }, installed: ['sdxl'] })
	];
	workersFail = false;
	installs = freshInstalls();
	const context = await browser.createBrowserContext();
	const { page, errors } = await openStudio(context, server);
	await page.waitForSelector('[data-testid="gpu-card"]');
	await settleAfterModels(page);
	await clickContinue(page);
	await waitForStep(page, 4);
	const start = await page.$('[data-testid="gpu-card-start"]');
	assert.equal(start !== null, realtime, '"Start drawing" shows only with a realtime model');
	if (realtime) {
		await page.click('[data-testid="gpu-card-start"]');
		await waitFor(() => page.url().includes('view=realtime_canvas'), 'the realtime canvas');
		await waitFor(async () => !(await cardOpen(page)), 'the card to close on Start drawing');
	}
	assert.deepEqual(errors, []);
	await context.close();
}

async function main() {
	const { server } = await serveStudioBuild();
	let browser;
	try {
		browser = await launchChrome({ args: ['--disable-dev-shm-usage'] });
		await scenarioAdminWithWorker(browser, server);
		await scenarioAdminNoWorkerKeyboard(browser, server);
		await scenarioEscapeDismiss(browser, server);
		await scenarioFailedList(browser, server);
		await scenarioAccessLostWhileOpen(browser, server);
		await scenarioViewer(browser, server);
		await scenarioNoneMode(browser, server);
		await scenarioBlockedStorage(browser, server);
		await scenarioModelDownloads(browser, server);
		await scenarioUnmanageableWorker(browser, server);
		await scenarioTokenGated(browser, server);
		await scenarioTourSwitchAndReopen(browser, server);
		await scenarioStartDrawing(browser, server, { realtime: false });
		await scenarioStartDrawing(browser, server, { realtime: true });
		console.log(
			'GPU card browser checks passed: default option, worker list, focus, both endings, dismissal, failed list, lost access, viewer, none mode, blocked storage, the tour order, the GPU and model steps with installs and progress, an unmanageable worker, a token-gated model, the tour switch, Finish setup, and Start drawing'
		);
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
}

await main();
