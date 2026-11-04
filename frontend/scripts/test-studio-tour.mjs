import assert from 'node:assert/strict';
import { access, mkdir, readFile, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

// The studio tour on the product build: first visit starts it, a finished tour
// stays shut, Help replays it, Escape skips with focus back on the control, and
// blocked storage never auto-starts it.
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
const storageKey = 'potocolom-tour-done';
const model = {
	id: 'tour-model',
	name: 'Tour model',
	capabilities: ['text_to_image'],
	min_vram_gb: 0,
	default: true,
	realtime_p95_ms: 100,
	estimated_gpu_ms_default: 100,
	parameters: { type: 'object', required: ['prompt'], properties: { prompt: { type: 'string' } } }
};
const account = {
	id: '550e8400-e29b-41d4-a716-446655440051',
	email: 'tour@example.com',
	role: 'admin'
};
const fixture = { models: [model] };
let accountMode = 'admin';
let releaseModels = null;
let modelsWaiting = false;
const english = JSON.parse(
	await readFile(new URL('../src/lib/i18n/en.json', import.meta.url), 'utf8')
);
const titles = ['canvas', 'prompt', 'model', 'generate', 'images'].map(
	(name) => english[`app.tour.${name}_title`]
);

function apiReply(pathname) {
	if (pathname === '/api/v1/account')
		return accountMode === 'local'
			? { status: 404, body: {} }
			: { status: 200, body: { ...account, role: accountMode } };
	if (pathname === '/api/v1/config') return { status: 200, body: { auth_methods: [] } };
	if (pathname === '/api/v1/models') return { status: 200, body: fixture.models };
	if (pathname === '/api/v1/generations/models')
		return { status: 200, body: { models: fixture.models } };
	return { status: 200, body: [] };
}

async function serveBuild() {
	const server = createServer(async (request, response) => {
		const url = new URL(request.url, 'http://localhost');
		if (url.pathname.startsWith('/api/')) {
			if (url.pathname === '/api/v1/models' && releaseModels) {
				await new Promise((resolveModels) => {
					releaseModels = resolveModels;
					modelsWaiting = true;
				});
			}
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

async function openStudio(
	context,
	server,
	{
		blockStorage = false,
		refuseWrites = false,
		tourDone = false,
		path = '/app',
		viewport = { width: 1280, height: 900 },
		locale = 'en'
	} = {}
) {
	const page = await context.newPage();
	page.setDefaultTimeout(timeoutMs);
	await page.setViewport(viewport);
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.message));
	await page.evaluateOnNewDocument(
		(blocked, refused, done, key, locale) => {
			if (locale !== 'en') localStorage.setItem('locale', locale);
			// The studio opens streams it never reads here; the tour does not use them.
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
			if (refused) {
				// Storage still reads, but every write fails.
				Storage.prototype.setItem = function setItem() {
					throw new Error('QuotaExceededError');
				};
			}
			if (done) {
				// Storage can be blocked: then there is no flag to set, and nothing to test.
				try {
					localStorage.setItem(key, '1');
				} catch {
					// Blocked storage: nothing to mark.
				}
			}
		},
		blockStorage,
		refuseWrites,
		tourDone,
		storageKey,
		locale
	);
	await page.goto(`http://127.0.0.1:${server.address().port}${path}`, {
		waitUntil: 'domcontentloaded'
	});
	return { page, errors };
}

const heading = (page) =>
	page.evaluate(() => document.querySelector('[data-studio-tour] h2')?.textContent.trim() ?? null);
const focusedText = (page) =>
	page.evaluate(() => document.activeElement?.textContent.trim() ?? null);
const focusedTour = (page) => page.evaluate(() => document.activeElement?.dataset.tour ?? null);
const tourOpen = async (page) => (await page.$('[data-studio-tour]')) !== null;
const hasButton = (page, label) =>
	page.evaluate(
		(text) => [...document.querySelectorAll('button')].some((b) => b.textContent.trim() === text),
		label
	);

async function clickButton(page, scope, label) {
	await page.evaluate(
		(query, text) => {
			const button = [...document.querySelectorAll(query)].find(
				(element) => element.textContent.trim() === text
			);
			if (!button) throw new Error(`No "${text}" button`);
			button.click();
		},
		scope,
		label
	);
}

// Moves the tour forward with Next until the heading shows `title`.
async function stepTo(page, title) {
	while ((await heading(page)) !== title) {
		const before = await heading(page);
		await clickButton(page, '[data-studio-tour] button', 'Next');
		await waitFor(async () => (await heading(page)) !== before, `a step after "${before}"`);
	}
}

// A phone-width viewport: the sidebar becomes a sheet, so Images leaves the page.
const phone = { width: 390, height: 844 };

async function main() {
	const server = await serveBuild();
	let browser;
	try {
		browser = await puppeteer.launch({
			headless: true,
			executablePath: await chromeExecutable(),
			args: ['--no-sandbox', '--disable-dev-shm-usage']
		});

		for (const regression of process.env.R3
			? [process.env.R3]
			: ['modal', 'sheet', 'keyboard', 'keyboard-es', 'phone']) {
			const context = await browser.createBrowserContext();
			if (regression === 'modal' || regression === 'sheet') {
				releaseModels = () => {};
				modelsWaiting = false;
			}
			const run = await openStudio(context, server, {
				viewport:
					regression === 'phone' || regression === 'sheet'
						? phone
						: regression.startsWith('keyboard')
							? { width: 1024, height: 500 }
							: { width: 1280, height: 900 },
				locale: regression === 'keyboard-es' ? 'es' : 'en'
			});
			if (regression === 'modal' || regression === 'sheet') {
				await waitFor(() => modelsWaiting, 'delayed models');
				if (regression === 'modal') {
					await run.page.keyboard.down('Control');
					await run.page.keyboard.press('k');
					await run.page.keyboard.up('Control');
					await run.page.waitForSelector('[role="dialog"][data-state="open"] input');
					await run.page.keyboard.type('test');
				} else {
					await clickButton(run.page, 'button', 'Toggle sidebar');
					await run.page.waitForSelector('[role="dialog"][data-state="open"]');
				}
				releaseModels();
				releaseModels = null;
				await pause(500);
				assert.equal(await tourOpen(run.page), false, 'automatic tour waits for open dialog');
				if (regression === 'modal') {
					await run.page.keyboard.type('ing');
					assert.equal(
						await run.page.$eval('[role="dialog"] input', (input) => input.value),
						'testing'
					);
				}
				await run.page.keyboard.press('Tab');
				assert.equal(
					await run.page.evaluate(() => !!document.activeElement.closest('[role="dialog"]')),
					true
				);
				if (regression === 'sheet') {
					await run.page.keyboard.down('Control');
					await run.page.keyboard.press('b');
					await run.page.keyboard.up('Control');
				} else await run.page.keyboard.press('Escape');
				await run.page.waitForSelector('[data-studio-tour]');
			} else {
				await run.page.waitForSelector('[data-studio-tour] h2');
				if (regression.startsWith('keyboard')) {
					assert.equal(
						await run.page.$eval('.tour-body', (el) => el.hasAttribute('tabindex')),
						false
					);
					await run.page.addStyleTag({ content: 'html { font-size: 32px !important; }' });
					await pause(300);
					await run.page.keyboard.press('Tab');
					assert.equal(
						await run.page.evaluate(() =>
							document.activeElement.matches('.tour-body[tabindex="0"][aria-labelledby]')
						),
						true,
						'overflowing body joins focus loop'
					);
					await run.page.keyboard.press('ArrowDown');
					await pause(200);
					assert.ok(
						await run.page.$eval('.tour-body', (el) => el.scrollTop > 0),
						'keyboard scrolls body'
					);
					await run.page.keyboard.down('Shift');
					await run.page.keyboard.press('Tab');
					await run.page.keyboard.up('Shift');
					assert.equal(
						await run.page.evaluate(() => document.activeElement.textContent.trim()),
						regression === 'keyboard-es' ? 'Siguiente' : 'Next'
					);
				} else {
					await pause(300);
					const geometry = await run.page.evaluate(() => {
						const ring = document.querySelector('[data-tour-ring]').getBoundingClientRect();
						const card = document.querySelector('[data-studio-tour]').getBoundingClientRect();
						return {
							height: ring.height,
							overlap: Math.max(
								0,
								Math.min(ring.bottom, card.bottom) - Math.max(ring.top, card.top)
							)
						};
					});
					assert.ok(geometry.height >= 200, `phone canvas visible height: ${geometry.height}`);
					assert.ok(geometry.overlap > 100, `card overlaps visible canvas: ${geometry.overlap}`);
				}
			}
			assert.deepEqual(run.errors, []);
			await context.close();
		}
		if (process.env.R3) return;

		for (const finding of process.env.TOUR_REGRESSION
			? [process.env.TOUR_REGRESSION]
			: ['2', '3', '4', '4es', '5']) {
			if (finding === '5') fixture.models = [];
			const context = await browser.createBrowserContext();
			const run = await openStudio(context, server, {
				viewport:
					finding === '2'
						? phone
						: finding.startsWith('4')
							? { width: 1024, height: 500 }
							: { width: 1280, height: 900 },
				locale: finding === '4es' ? 'es' : 'en'
			});
			await run.page.waitForSelector('[data-studio-tour] h2');
			if (finding === '2') {
				await run.page.keyboard.down('Control');
				await run.page.keyboard.press('k');
				await run.page.keyboard.press('b');
				await run.page.keyboard.up('Control');
				await run.page.keyboard.down('Meta');
				await run.page.keyboard.press('k');
				await run.page.keyboard.press('b');
				await run.page.keyboard.up('Meta');
				await pause(300);
				assert.equal(
					await run.page.$$eval('[role="dialog"]', (els) => els.length),
					1,
					'shortcuts must not open another dialog'
				);
				await run.page.keyboard.press('Tab');
				await run.page.keyboard.down('Shift');
				await run.page.keyboard.press('Tab');
				await run.page.keyboard.up('Shift');
				assert.equal(
					await run.page.evaluate(() =>
						document.querySelector('[data-studio-tour]').contains(document.activeElement)
					),
					true
				);
				await run.page.keyboard.press('Escape');
				assert.equal(await tourOpen(run.page), false);
			} else if (finding === '3') {
				await stepTo(run.page, titles[4]);
				await run.page.evaluate(() =>
					[...document.querySelectorAll('button')]
						.find((button) => button.getAttribute('aria-keyshortcuts') === 'Meta+B Control+B')
						.click()
				);
				await pause(500);
				const delta = await run.page.evaluate(() => {
					const target = document.querySelector('[data-tour="images"]').getBoundingClientRect();
					const ring = document.querySelector('[data-tour-ring]').getBoundingClientRect();
					return Math.abs(target.left - ring.left) + Math.abs(target.width - ring.width);
				});
				assert.ok(delta < 3, `ring follows sidebar transition: ${delta}`);
				await run.page.evaluate(() => {
					const target = document.querySelector('[data-tour="images"]');
					const wrapper = document.createElement('div');
					wrapper.style.cssText = 'height: 32px; overflow: auto';
					target.parentNode.insertBefore(wrapper, target);
					wrapper.append(target);
					const spacer = document.createElement('div');
					spacer.style.height = '100px';
					wrapper.prepend(spacer);
				});
				await pause(150);
				await run.page.evaluate(() => {
					document.querySelector('[data-tour="images"]').parentElement.scrollTop = 80;
				});
				await pause(150);
				const scrollDelta = await run.page.evaluate(() => {
					const target = document.querySelector('[data-tour="images"]').getBoundingClientRect();
					const ring = document.querySelector('[data-tour-ring]').getBoundingClientRect();
					return Math.abs(target.top - ring.top);
				});
				assert.ok(scrollDelta < 3, `ring follows nested scroll: ${scrollDelta}`);
				await run.page.evaluate(() => {
					document.querySelector('[data-tour="images"]').style.height = '50px';
				});
				await pause(150);
				const sizeDelta = await run.page.evaluate(() =>
					Math.abs(
						document.querySelector('[data-tour="images"]').getBoundingClientRect().height -
							document.querySelector('[data-tour-ring]').getBoundingClientRect().height
					)
				);
				assert.ok(sizeDelta < 3, `ring follows target resize: ${sizeDelta}`);
			} else if (finding.startsWith('4')) {
				await run.page.addStyleTag({ content: 'html { font-size: 32px !important; }' });
				await pause(300);
				const inside = await run.page.evaluate(() =>
					[...document.querySelectorAll('[data-studio-tour] button')].every((button) => {
						const r = button.getBoundingClientRect();
						return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
					})
				);
				assert.equal(inside, true, 'all tour buttons remain in the viewport with enlarged text');
				assert.equal(
					await run.page.$eval('.tour-body', (body) => body.scrollHeight > body.clientHeight),
					true,
					'long tour text overflows'
				);
				const shots =
					'/tmp/claude-1000/-home-leon-Nextcloud-ETSIIT-ETSHIT-Github-potocolom/c7eba735-0ba3-4fd0-ba50-94f893f5f5e0/scratchpad/tour-shots';
				await mkdir(shots, { recursive: true });
				await run.page.screenshot({ path: `${shots}/r2-1024x500-${finding}.png` });
			} else if (finding === '5') {
				await clickButton(run.page, '[data-studio-tour] button', 'Next');
				await waitFor(
					async () => (await heading(run.page)) === english['app.tour.install_title'],
					'install step'
				);
				fixture.models = [model];
				await run.page.evaluate(() => {
					const original = Date.now;
					Date.now = () => original() + 3600000;
					document.dispatchEvent(new Event('visibilitychange'));
				});
				await run.page.waitForSelector('[data-tour="prompt"]');
				await pause(300);
				assert.notEqual(
					await heading(run.page),
					english['app.tour.install_title'],
					'install step becomes ineligible when models arrive'
				);
				assert.doesNotMatch(
					await run.page.$eval('[data-studio-tour] p', (el) => el.textContent),
					/Step 0/
				);
			}
			await context.close();
			fixture.models = [model];
		}
		if (process.env.TOUR_REGRESSION) return;

		// Viewers cannot generate, so neither the automatic tour nor its entry is available.
		accountMode = 'viewer';
		for (const path of ['/app', '/app?view=account', '/app?view=images']) {
			const viewer = await browser.createBrowserContext();
			const run = await openStudio(viewer, server, { path, viewport: phone });
			await waitFor(() => hasButton(run.page, 'Toggle sidebar'), 'the viewer phone header');
			await pause(800);
			assert.equal(await tourOpen(run.page), false, 'viewer must not get an automatic tour');
			await clickButton(run.page, 'button', 'Toggle sidebar');
			await run.page.waitForSelector('[data-tour="images"]');
			assert.equal(await hasButton(run.page, 'Show tour'), false, 'viewer has no tour entry');
			assert.equal(await run.page.evaluate((key) => localStorage.getItem(key), storageKey), null);
			assert.deepEqual(run.errors, []);
			await viewer.close();
		}

		// A user gets the automatic tour and can replay it from the sidebar.
		accountMode = 'user';
		const user = await browser.createBrowserContext();
		const userRun = await openStudio(user, server);
		await userRun.page.waitForSelector('[data-studio-tour] h2');
		assert.equal(await heading(userRun.page), titles[0]);
		await clickButton(userRun.page, '[data-studio-tour] button', 'Skip tour');
		await clickButton(userRun.page, 'button', 'Show tour');
		await waitFor(async () => (await heading(userRun.page)) === titles[0], 'user replay');
		assert.deepEqual(userRun.errors, []);
		await user.close();

		accountMode = 'local';
		const local = await browser.createBrowserContext();
		const localRun = await openStudio(local, server);
		await localRun.page.waitForSelector('[data-studio-tour] h2');
		await clickButton(localRun.page, '[data-studio-tour] button', 'Skip tour');
		assert.equal(await hasButton(localRun.page, 'Show tour'), true, 'local admin has tour entry');
		await local.close();
		accountMode = 'admin';

		// A page change can remove the tour controls after the user has focused a new field.
		const changed = await browser.createBrowserContext();
		const changedRun = await openStudio(changed, server);
		await changedRun.page.waitForSelector('[data-studio-tour] h2');
		await stepTo(changedRun.page, titles[4]);
		await changedRun.page.evaluate(() => {
			const input = document.createElement('input');
			input.id = 'new-view-input';
			document.body.append(input);
			input.focus();
		});
		await changedRun.page.keyboard.type('first');
		await changedRun.page.evaluate(() => {
			document.querySelector('[data-tour="images"]').remove();
		});
		await waitFor(
			async () => !(await tourOpen(changedRun.page)),
			'tour to close after page change'
		);
		assert.equal(
			await changedRun.page.evaluate(() => document.activeElement?.id),
			'new-view-input',
			'the tour must preserve focus in the new view'
		);
		await changedRun.page.keyboard.type(' second');
		assert.equal(
			await changedRun.page.$eval('#new-view-input', (input) => input.value),
			'first second'
		);
		assert.deepEqual(changedRun.errors, []);
		await changed.close();

		// First visit: five steps, Next focused, Done closes it, flag set, focus on Images.
		const first = await browser.createBrowserContext();
		const { page, errors } = await openStudio(first, server);
		await page.waitForSelector('[data-studio-tour] h2');
		// The card is a named dialog; its body is the description; each step is in a live region.
		const semantics = await page.evaluate(() => {
			const card = document.querySelector('[data-studio-tour]');
			const named = (id) => document.getElementById(id)?.textContent.trim() ?? null;
			return {
				role: card.getAttribute('role'),
				name: named(card.getAttribute('aria-labelledby')),
				description: named(card.getAttribute('aria-describedby')),
				live: card.querySelector('[aria-live="polite"]') !== null
			};
		});
		assert.equal(semantics.role, 'dialog');
		assert.equal(semantics.name, titles[0]);
		assert.equal(semantics.description, english['app.tour.canvas_body']);
		assert.equal(semantics.live, true, 'step changes are announced');
		for (const [index, title] of titles.entries()) {
			const last = index === titles.length - 1;
			await waitFor(async () => (await heading(page)) === title, `step ${index + 1}`);
			assert.equal(await focusedText(page), last ? 'Done' : 'Next');
			await clickButton(page, '[data-studio-tour] button', last ? 'Done' : 'Next');
		}
		await waitFor(async () => !(await tourOpen(page)), 'the tour to close');
		assert.equal(await page.evaluate((key) => localStorage.getItem(key), storageKey), '1');
		assert.equal(await focusedTour(page), 'images', 'focus returns to the last control');
		assert.deepEqual(errors, []);

		// Second visit: the flag is set, so the tour does not start again.
		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.waitForSelector('[data-tour="prompt"]');
		await pause(800);
		assert.equal(await tourOpen(page), false, 'a finished tour must not auto-start');

		// Leaving during a replay must not carry the open request back into the studio.
		await clickButton(page, 'button', 'Show tour');
		await waitFor(async () => (await heading(page)) === titles[0], 'replayed tour before leaving');
		await page.evaluate(() => {
			const link = document.createElement('a');
			link.href = '/setup';
			document.body.append(link);
			link.click();
		});
		await waitFor(() => new URL(page.url()).pathname === '/setup', 'navigation to setup');
		await page.goBack({ waitUntil: 'domcontentloaded' });
		await page.waitForSelector('[data-tour="prompt"]');
		await pause(800);
		assert.equal(await tourOpen(page), false, 'leaving a replay must clear its open request');

		// Help replays it; Escape skips and returns focus to the step's control.
		await clickButton(page, 'button', 'Show tour');
		await waitFor(async () => (await heading(page)) === titles[0], 'replayed tour');
		await page.keyboard.press('Escape');
		await waitFor(async () => !(await tourOpen(page)), 'Escape to close the tour');
		assert.equal(await focusedTour(page), 'canvas', 'Escape returns focus to the canvas');
		await first.close();

		// Fresh installs still explain where to install a model.
		fixture.models = [];
		const sparse = await browser.createBrowserContext();
		const sparseRun = await openStudio(sparse, server);
		await sparseRun.page.waitForSelector('[data-studio-tour] h2');
		await clickButton(sparseRun.page, '[data-studio-tour] button', 'Next');
		await waitFor(
			async () => (await heading(sparseRun.page)) === english['app.tour.install_title'],
			'install step'
		);
		assert.equal(
			await sparseRun.page.$eval('[data-studio-tour] p', (el) => el.textContent.trim()),
			'Step 2 of 3'
		);
		await sparseRun.page.keyboard.press('Escape');
		assert.equal(await focusedTour(sparseRun.page), 'install-model');
		await clickButton(sparseRun.page, 'button', 'Open Models');
		await waitFor(
			() => new URL(sparseRun.page.url()).searchParams.get('view') === 'models',
			'Models view'
		);
		await sparse.close();
		const sparsePhone = await browser.createBrowserContext();
		const sparsePhoneRun = await openStudio(sparsePhone, server, { viewport: phone });
		await sparsePhoneRun.page.waitForSelector('[data-studio-tour] h2');
		await clickButton(sparsePhoneRun.page, '[data-studio-tour] button', 'Next');
		await waitFor(
			async () => (await heading(sparsePhoneRun.page)) === english['app.tour.install_title'],
			'phone install step'
		);
		assert.match(
			await sparsePhoneRun.page.$eval('#studio-tour-body', (el) => el.textContent),
			/Open the menu to find Models/
		);
		await sparsePhone.close();
		fixture.models = [model];

		// Check every visible step at desktop, tablet and phone sizes.
		const shots =
			'/tmp/claude-1000/-home-leon-Nextcloud-ETSIIT-ETSHIT-Github-potocolom/c7eba735-0ba3-4fd0-ba50-94f893f5f5e0/scratchpad/tour-shots';
		await mkdir(shots, { recursive: true });
		for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, phone]) {
			const context = await browser.createBrowserContext();
			const run = await openStudio(context, server, { viewport });
			await run.page.waitForSelector('[data-studio-tour] h2');
			for (let i = 0; i < titles.length; i++) {
				await waitFor(async () => (await heading(run.page)) === titles[i], `step ${i + 1}`);
				await pause(150);
				const geometry = await run.page.evaluate(
					(i, total) => {
						const card = document.querySelector('[data-studio-tour]').getBoundingClientRect();
						const ring = document.querySelector('[data-tour-ring]').getBoundingClientRect();
						const names = ['canvas', 'prompt', 'model', 'generate', 'images'];
						let target = document.querySelector(`[data-tour="${names[i]}"]`);
						if (!target || !target.getClientRects().length)
							target = [...document.querySelectorAll('[data-sidebar="trigger"]')].find(
								(el) => el.getClientRects().length > 0
							);
						const r = target.getBoundingClientRect();
						return {
							inside:
								card.left >= 0 &&
								card.top >= 0 &&
								card.right <= innerWidth &&
								card.bottom <= innerHeight,
							ring:
								Math.abs(ring.left - Math.max(0, r.left)) < 2 &&
								Math.abs(ring.top - Math.max(0, r.top)) < 2 &&
								Math.abs(ring.right - Math.min(innerWidth, r.right)) < 2 &&
								Math.abs(ring.bottom - Math.min(innerHeight, r.bottom)) < 2,
							counter:
								document.querySelector('[data-studio-tour] p').textContent.trim() ===
								`Step ${i + 1} of ${total}`
						};
					},
					i,
					titles.length
				);
				assert.deepEqual(
					geometry,
					{ inside: true, ring: true, counter: true },
					`${viewport.width} step ${i + 1}`
				);
				if (i === 4 && viewport.width === 390)
					assert.match(
						await run.page.$eval('#studio-tour-body', (el) => el.textContent),
						/Open the menu to find Images/
					);
				await run.page.screenshot({
					path: `${shots}/${i === 0 ? 'r3' : 'r2'}-${viewport.width}x${viewport.height}-step${i + 1}.png`
				});
				if (i < titles.length - 1) await clickButton(run.page, '[data-studio-tour] button', 'Next');
			}
			await run.page.keyboard.press('Escape');
			await waitFor(async () => !(await tourOpen(run.page)), 'Escape');
			assert.equal(
				await run.page.evaluate(
					() => document.activeElement?.dataset.tour ?? document.activeElement?.dataset.sidebar
				),
				viewport.width === 390 ? 'trigger' : 'images'
			);
			assert.deepEqual(run.errors, []);
			await context.close();
		}

		// Storage that reads but refuses writes: finishing could not save the flag,
		// so the tour must not auto-start at all.
		const unwritable = await browser.createBrowserContext();
		const unwritableRun = await openStudio(unwritable, server, { refuseWrites: true });
		await unwritableRun.page.waitForSelector('[data-tour="prompt"]');
		await pause(800);
		assert.equal(
			await tourOpen(unwritableRun.page),
			false,
			'unwritable storage must not auto-start'
		);
		await unwritable.close();

		// Generate is disabled until there is a prompt, so on this step the anchor cannot
		// take focus. Escape must leave focus on the prompt field, not on the page body.
		const empty = await browser.createBrowserContext();
		const emptyRun = await openStudio(empty, server);
		await emptyRun.page.waitForSelector('[data-studio-tour] h2');
		await stepTo(emptyRun.page, titles[3]);
		assert.equal(
			await emptyRun.page.$eval('[data-tour="generate"]', (button) => button.disabled),
			true,
			'Generate is disabled with an empty prompt'
		);
		await emptyRun.page.keyboard.press('Escape');
		await waitFor(async () => !(await tourOpen(emptyRun.page)), 'Escape on the Generate step');
		assert.equal(
			await emptyRun.page.evaluate(() => document.activeElement?.id ?? null),
			'gen-prompt',
			'focus falls back to the prompt field'
		);
		assert.equal(await emptyRun.page.evaluate((key) => localStorage.getItem(key), storageKey), '1');
		assert.deepEqual(emptyRun.errors, []);
		await empty.close();

		// Blocked storage: no auto-start, but Help still plays the tour.
		const blocked = await browser.createBrowserContext();
		const blockedRun = await openStudio(blocked, server, { blockStorage: true });
		await blockedRun.page.waitForSelector('[data-tour="prompt"]');
		await pause(800);
		assert.equal(await tourOpen(blockedRun.page), false, 'blocked storage must not auto-start');
		await clickButton(blockedRun.page, 'button', 'Show tour');
		await waitFor(async () => (await heading(blockedRun.page)) === titles[0], 'tour from Help');
		await blocked.close();

		// A short window: the options column scrolls, and the model picker sits above the
		// fold once it is scrolled to its end. The next step must bring the picker into
		// view, and place the card with it.
		const short = await browser.createBrowserContext();
		const shortRun = await openStudio(short, server, {
			viewport: { width: 1280, height: 400 }
		});
		await shortRun.page.waitForSelector('[data-studio-tour] h2');
		await stepTo(shortRun.page, titles[1]);
		await shortRun.page.evaluate(() => {
			const column = document.querySelector('[data-tour="generate"]').closest('form');
			const scroller = column.querySelector('.overflow-y-auto');
			scroller.scrollTop = scroller.scrollHeight;
		});
		await clickButton(shortRun.page, '[data-studio-tour] button', 'Next');
		await waitFor(async () => (await heading(shortRun.page)) === titles[2], 'the model step');
		const placed = await shortRun.page.evaluate(() => {
			const control = document.querySelector('[data-tour="model"]').getBoundingClientRect();
			const card = document.querySelector('[data-studio-tour]').getBoundingClientRect();
			return {
				control: control.top >= 0 && control.bottom <= innerHeight,
				card: card.bottom > 0 && card.top < innerHeight
			};
		});
		assert.deepEqual(
			placed,
			{ control: true, card: true },
			'the model picker is scrolled into view'
		);
		assert.deepEqual(shortRun.errors, []);
		await short.close();

		// Leave Generate while models are loading, before the first card can show.
		const interrupted = await browser.createBrowserContext();
		releaseModels = () => {};
		const interruptedRun = await openStudio(interrupted, server);
		await interruptedRun.page.waitForSelector('[data-tour="images"]');
		await waitFor(() => modelsWaiting, 'the delayed models request');
		await interruptedRun.page.click('[data-tour="images"]');
		await waitFor(
			() => new URL(interruptedRun.page.url()).searchParams.get('view') === 'images',
			'Images before models answer'
		);
		releaseModels();
		releaseModels = null;
		await waitFor(
			() => new URL(interruptedRun.page.url()).searchParams.get('view') === 'images',
			'Images'
		);
		await pause(800);
		assert.equal(
			new URL(interruptedRun.page.url()).searchParams.get('view'),
			'images',
			'the chosen view stays open'
		);
		assert.equal(await tourOpen(interruptedRun.page), false, 'no tour card appears');
		assert.equal(
			await interruptedRun.page.evaluate((key) => localStorage.getItem(key), storageKey),
			null
		);
		await clickButton(interruptedRun.page, 'button', 'Generate');
		await waitFor(
			async () => (await heading(interruptedRun.page)) === titles[0],
			'tour after returning to Generate'
		);
		assert.deepEqual(interruptedRun.errors, []);
		await interrupted.close();

		// A user's first visit on Images stays there; the tour starts on the later visit to Generate.
		accountMode = 'user';
		const deep = await browser.createBrowserContext();
		const deepRun = await openStudio(deep, server, { path: '/app?view=images' });
		await waitFor(() => hasButton(deepRun.page, 'Show tour'), 'the studio sidebar');
		await pause(800);
		assert.equal(await tourOpen(deepRun.page), false, 'Images must not auto-start the tour');
		assert.equal(new URL(deepRun.page.url()).searchParams.get('view'), 'images');
		assert.equal(await deepRun.page.evaluate((key) => localStorage.getItem(key), storageKey), null);
		await clickButton(deepRun.page, 'button', 'Generate');
		await waitFor(async () => (await heading(deepRun.page)) === titles[0], 'tour on Generate');
		assert.equal(new URL(deepRun.page.url()).searchParams.get('view'), null);
		assert.deepEqual(deepRun.errors, []);
		await deep.close();
		accountMode = 'admin';

		// Show tour from Images on a phone: the sheet closes, and the tour opens Generate.
		const phoneHelp = await browser.createBrowserContext();
		const phoneRun = await openStudio(phoneHelp, server, {
			path: '/app?view=images',
			viewport: phone,
			tourDone: true
		});
		await waitFor(() => hasButton(phoneRun.page, 'Toggle sidebar'), 'the phone header');
		await clickButton(phoneRun.page, 'button', 'Toggle sidebar');
		await waitFor(() => hasButton(phoneRun.page, 'Show tour'), 'Show tour in the sheet');
		await clickButton(phoneRun.page, 'button', 'Show tour');
		await waitFor(
			async () => (await heading(phoneRun.page)) === titles[0],
			'the replayed tour to start on Generate'
		);
		assert.deepEqual(phoneRun.errors, []);
		await phoneHelp.close();

		console.log(
			'studio tour browser checks passed: spotlight, placement, counters, empty models, focus, replay and storage'
		);
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
}

await main();
