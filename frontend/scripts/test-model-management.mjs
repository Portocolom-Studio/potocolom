import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { readFile, stat, access } from 'node:fs/promises';
import { resolve, join, extname } from 'node:path';
import puppeteer from 'puppeteer-core';
import { recordPageEvents } from './studio-test-page.mjs';

const root = resolve('build');
const gib = 1024 ** 3;
const model = (id, extra = {}) => ({
	id,
	name: id,
	capabilities: ['text_to_image'],
	tier: 'standard',
	license_id: 'MIT',
	min_vram_gb: 8,
	installed: false,
	download_bytes: gib,
	needs_token: false,
	fits: true,
	suggested: false,
	download: null,
	...extra
});
const worker = {
	worker_id: 'worker-1',
	manageable: true,
	hardware: {
		gpu_name: 'GPU',
		vram_total_bytes: 8 * gib,
		disk_total_bytes: 20 * gib,
		disk_free_bytes: 7 * gib
	},
	disk_free_bytes: 7 * gib,
	models: [
		model('suggested', { suggested: true }),
		model('large', { fits: false, min_vram_gb: 16, download_bytes: 2 * gib }),
		model('installed', { installed: true }),
		model('gated', { needs_token: true })
	]
};
const calls = [];
let role = 'admin';
let localMode = false;
let removalPolls = 0;
let retryPolls = 0;
let installs = 0;
let failNextInstall = false;
let workerGone = false;
let delayNext = false;
let releaseDelayed;
let opSequence = 0;
let retryOp;
let removalOp;
let refuseRemoval = false;
let silentInstall = false;
let holdInstalls = false;
let cancelNotRunning = false;
let failInventory = process.env.MODEL_REGRESSION === 'error-focus';
const regression = process.env.MODEL_REGRESSION;
let awaitingInventory = !regression;
const op = (n) => `${String(n).padStart(20, '0')}-00000000`;
const types = {
	'.html': 'text/html',
	'.js': 'application/javascript',
	'.css': 'text/css',
	'.json': 'application/json',
	'.woff2': 'font/woff2',
	'.svg': 'image/svg+xml'
};
test('model management selection, disk margin, progress, cancel and removal', async () => {
	const server = createServer(async (req, res) => {
		const path = new URL(req.url, 'http://localhost').pathname;
		if (path.startsWith('/api/')) {
			calls.push(`${req.method} ${path}`);
			let body = [];
			if (path === '/api/v1/account')
				body = localMode
					? null
					: {
							id: '11000000-0000-4000-8000-000000000001',
							email: 'admin@example.test',
							role
						};
			else if (path === '/api/v1/config')
				body = { auth_methods: localMode ? [] : ['password'], billing_enabled: false };
			else if (path === '/api/v1/studio/gpu' && (workerGone || regression === 'panel-protocol6')) {
				res
					.writeHead(503, { 'Content-Type': 'application/json' })
					.end(JSON.stringify({ detail: 'no worker connected' }));
				return;
			} else if (path === '/api/v1/admin/models') {
				if (failInventory) {
					failInventory = false;
					res.writeHead(500).end();
					return;
				}
				if (delayNext) {
					delayNext = false;
					const snapshot = JSON.stringify([worker]);
					await new Promise((resolve) => {
						releaseDelayed = resolve;
					});
					res.writeHead(200, { 'Content-Type': 'application/json' }).end(snapshot);
					return;
				}
				if (removalPolls > 0 && --removalPolls === 0) {
					worker.models[2].installed = false;
					worker.models[2].download = {
						kind: 'remove',
						state: 'done',
						bytes_done: 0,
						bytes_total: 0,
						error: null,
						op_id: removalOp
					};
				}
				if (retryPolls > 0 && --retryPolls === 0)
					worker.models[0].download = {
						kind: 'install',
						state: 'downloading',
						bytes_done: gib / 2,
						bytes_total: gib,
						error: null,
						op_id: retryOp
					};
				body = workerGone
					? []
					: [awaitingInventory ? { ...worker, manageable: false, models: [] } : worker];
			} else if (path.endsWith('/install')) {
				const opId = op(++opSequence);
				body = { op_id: opId };
				installs++;
				if (failNextInstall) {
					failNextInstall = false;
					res
						.writeHead(409, { 'Content-Type': 'application/json' })
						.end(JSON.stringify({ detail: 'busy right now' }));
					return;
				}
				if (holdInstalls) {
					// Keep the previous record until the test publishes a result.
				} else if (silentInstall) {
					silentInstall = false;
				} else if (installs > 1) {
					retryOp = opId;
					retryPolls = 6;
					worker.models[0].download = {
						kind: 'install',
						state: 'failed',
						bytes_done: 0,
						bytes_total: gib,
						error: 'download_failed',
						op_id: op(0)
					};
				} else
					worker.models[0].download = {
						kind: 'install',
						state: 'downloading',
						bytes_done: gib / 2,
						bytes_total: gib,
						error: null,
						op_id: opId
					};
			} else if (path.endsWith('/cancel')) {
				const opId = op(++opSequence);
				body = { op_id: opId };
				worker.models[2].download = {
					kind: 'remove',
					state: 'failed',
					bytes_done: 0,
					bytes_total: 0,
					error: 'remove_failed',
					op_id: op(0)
				};
				worker.models[0].download = {
					kind: 'cancel',
					state: 'failed',
					bytes_done: gib / 2,
					bytes_total: gib,
					error: cancelNotRunning ? 'not_running' : 'cancelled',
					op_id: opId
				};
			} else if (req.method === 'DELETE') {
				removalOp = op(++opSequence);
				body = { op_id: removalOp };
				if (refuseRemoval)
					worker.models[2].download = {
						kind: 'remove',
						state: 'failed',
						bytes_done: 0,
						bytes_total: 0,
						error: 'in_use',
						op_id: removalOp
					};
				else {
					removalPolls = regression === 'later-active' || regression === 'later-failed' ? 0 : 3;
					if (regression === 'removal')
						worker.models[2].download = {
							kind: 'remove',
							state: 'queued',
							bytes_done: 0,
							bytes_total: 0,
							error: null,
							op_id: removalOp
						};
				}
			}
			res
				.writeHead(path === '/api/v1/account' && localMode ? 404 : body?.op_id ? 202 : 200, {
					'Content-Type': 'application/json'
				})
				.end(JSON.stringify(body));
			return;
		}
		const file = resolve(root, `.${decodeURIComponent(path)}`);
		if (!file.startsWith(root)) {
			res.writeHead(404).end();
			return;
		}
		for (const candidate of [
			file,
			`${file}.html`,
			join(file, 'index.html'),
			join(root, '200.html')
		]) {
			if (
				await stat(candidate).then(
					(s) => s.isFile(),
					() => false
				)
			) {
				res
					.writeHead(200, {
						'Content-Type': types[extname(candidate)] ?? 'application/octet-stream'
					})
					.end(await readFile(candidate));
				return;
			}
		}
		res.writeHead(404).end();
	});
	await new Promise((done) => server.listen(0, '127.0.0.1', done));
	let browser;
	try {
		const candidates = [
			process.env.PUPPETEER_EXECUTABLE_PATH,
			process.env.CHROME_PATH,
			process.env.CHROME_BIN,
			'/usr/bin/google-chrome',
			'/usr/bin/chromium',
			'/usr/bin/chromium-browser'
		].filter(Boolean);
		let executablePath;
		for (const candidate of candidates)
			if (
				await access(candidate).then(
					() => true,
					() => false
				)
			) {
				executablePath = candidate;
				break;
			}
		assert.ok(executablePath, 'Chrome is required');
		browser = await puppeteer.launch({
			executablePath,
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		recordPageEvents(page);
		await page.setViewport({ width: 1440, height: 900 });
		if (regression === 'panel-viewer') role = 'viewer';
		if (regression === 'panel-offline' || regression === 'panel-viewer-offline') workerGone = true;
		if (regression === 'panel-viewer-offline') role = 'viewer';
		if (regression === 'panel-protocol6')
			worker.models.forEach((entry) => (entry.installed = false));
		if (regression === 'gated-button')
			worker.models[1].download = {
				kind: 'install',
				state: 'downloading',
				bytes_done: 0,
				bytes_total: gib,
				error: null,
				op_id: op(0)
			};
		if (regression === 'footer-focus') {
			worker.models.unshift(...Array.from({ length: 12 }, (_, i) => model(`preceding-${i}`)));
		}
		if (regression === 'reserved') {
			worker.disk_free_bytes = 12 * gib;
			worker.models[1].download = {
				kind: 'install',
				state: 'downloading',
				bytes_done: 0,
				bytes_total: 6 * gib,
				error: null,
				op_id: op(0)
			};
		}
		if (regression === 'retry' || regression === 'failure') {
			holdInstalls = true;
			worker.models[0].download = {
				kind: 'install',
				state: 'failed',
				bytes_done: 9 * gib,
				bytes_total: 10 * gib,
				error: 'download_failed',
				op_id: op(0)
			};
			worker.disk_free_bytes = 8 * gib;
		}
		if (regression === 'remount-remove' || regression === 'retry-remove') {
			worker.models[2].installed = regression !== 'retry-remove';
			worker.models[2].download = {
				kind: 'remove',
				state: regression === 'remount-remove' ? 'queued' : 'failed',
				bytes_done: 0,
				bytes_total: 0,
				error: regression === 'retry-remove' ? 'remove_failed' : null,
				op_id: op(0)
			};
		}
		if (regression === 'spanish-labels')
			worker.models[0].capabilities = ['text_to_image', 'image_to_image', 'upscale', 'realtime'];
		if (regression === 'unknown-reservation') {
			worker.models[1].download_bytes = null;
			worker.models[1].download = {
				kind: 'install',
				state: 'queued',
				bytes_done: 0,
				bytes_total: 0,
				error: null,
				op_id: op(0)
			};
		}
		if (regression === 'unknown-space') {
			worker.disk_free_bytes = null;
			worker.hardware.disk_free_bytes = null;
		}
		if (regression === 'spanish' || regression === 'spanish-labels' || regression === 'locale-size')
			await page.evaluateOnNewDocument(() => localStorage.setItem('locale', 'es'));
		await page.evaluateOnNewDocument(() => localStorage.setItem('potocolom-tour-done', '1'));
		const panelResponse =
			regression === 'panel-protocol6'
				? page
						.waitForResponse((response) => response.url().endsWith('/api/v1/admin/models'), {
							timeout: 0
						})
						.catch(() => null)
				: null;
		await page.goto(
			`http://127.0.0.1:${server.address().port}/app?view=${regression?.startsWith('panel-') ? 'models' : 'admin&tab=models'}`,
			{
				waitUntil: 'domcontentloaded'
			}
		);
		if (regression?.startsWith('panel-')) {
			if (panelResponse) {
				await Promise.race([
					panelResponse,
					page
						.waitForFunction(() => document.body.textContent.includes('Start a worker'), {
							timeout: 0
						})
						.catch(() => null)
				]);
				assert.ok(
					await page.$('::-p-text(Install models)'),
					'connected worker offers installation'
				);
			}
			await page.waitForFunction(
				() =>
					document.body.textContent.includes('No models installed yet.') ||
					document.body.textContent.includes('Start a worker')
			);
			if (regression === 'panel-offline') {
				await page.waitForFunction(() => document.body.textContent.includes('Start a worker'));
				assert.equal(await page.$('::-p-text(Install models)'), null);
			} else if (regression === 'panel-viewer' || regression === 'panel-viewer-offline') {
				assert.match(
					await page.evaluate(() => document.body.textContent),
					/No models installed yet. Ask an administrator to install one./
				);
				assert.equal(await page.$('::-p-text(Install models)'), null);
				assert.equal(calls.filter((c) => c === 'GET /api/v1/studio/gpu').length, 0);
			} else {
				const button = await page.waitForSelector('::-p-text(Install models)', { timeout: 2000 });
				await button.click();
				await page.waitForFunction(
					() => location.search.includes('view=admin') && location.search.includes('tab=models')
				);
				await page.waitForSelector('[data-worker="worker-1"]');
			}
			return;
		}
		if (regression === 'error-focus') {
			const retry = await page.waitForSelector('::-p-text(Retry)');
			await retry.focus();
			await page.waitForSelector('[data-model="suggested"]', { timeout: 15000 });
			assert.equal(await page.evaluate(() => document.activeElement !== document.body), true);
			return;
		}
		if (!regression) {
			await page.waitForSelector('[data-worker="worker-1"]');
			assert.equal(await page.$('[data-model]'), null);
			worker.models[2].installed = false;
			awaitingInventory = false;
			await page.waitForSelector('[data-model="suggested"] input', { timeout: 15000 });
			assert.equal(await page.$eval('[data-model="suggested"] input', (el) => el.checked), true);
			worker.models[2].installed = true;
			await page.reload({ waitUntil: 'domcontentloaded' });
		}
		await page.waitForSelector('[data-model="suggested"] input');
		if (regression === 'unknown-reservation') {
			await page.click('[data-model="suggested"] input');
			assert.match(
				await page.$eval('[data-selection-footer]', (el) => el.textContent),
				/Free after download: Unknown/
			);
			assert.match(
				await page.$eval('[data-selection-footer]', (el) => el.textContent),
				/queued or active download has an unknown size/
			);
			assert.equal(await page.$eval('[data-selection-footer] button', (el) => el.disabled), true);
			assert.equal(
				await page.$eval('[data-model="suggested"] button', (el) =>
					el.getAttribute('aria-disabled')
				),
				'true'
			);
			assert.match(await page.$eval('[data-model="large"]', (el) => el.textContent), /Unknown/);
			return;
		}
		if (regression === 'spanish-labels') {
			const text = await page.$eval('[data-model-list]', (el) => el.textContent);
			assert.match(text, /Texto a imagen/);
			assert.match(text, /Imagen a imagen/);
			assert.match(text, /Ampliar/);
			assert.match(text, /Tiempo real/);
			assert.match(text, /Estándar/);
			assert.doesNotMatch(
				text,
				/Text to image|text to image|standard|Download selected|Not installed|Suggested|Install|Remove|Worker disk/
			);
			return;
		}
		if (regression === 'dismiss-focus') {
			failNextInstall = true;
			await page.$eval('[data-model="suggested"] button', (el) => el.focus());
			await page.keyboard.press('Enter');
			await page.waitForFunction(() =>
				document.querySelector('[data-model="suggested"] [role="alert"] button')
			);
			await page.$eval('[data-model="suggested"] [role="alert"] button', (el) => el.focus());
			await page.keyboard.press('Enter');
			await page.waitForFunction(
				() => !document.querySelector('[data-model="suggested"] [role="alert"] button')
			);
			assert.equal(
				await page.evaluate(() =>
					document.activeElement.matches('[data-model="suggested"] [data-heading]')
				),
				true
			);
			return;
		}
		if (regression === 'locale-size') {
			assert.match(await page.$eval('[data-model="large"]', (el) => el.textContent), /2,00 GiB/);
			assert.match(await page.$eval('[data-disk]', (el) => el.textContent), /7,00 GiB/);
			return;
		}
		if (regression === 'unknown-space') {
			await page.click('[data-model="large"] input');
			assert.match(
				await page.$eval('[data-selection-footer]', (el) => el.textContent),
				/Selected total: 2.00 GiB/
			);
			assert.equal(await page.$eval('[data-selection-footer] button', (el) => el.disabled), true);
			return;
		}
		if (regression === 'failed-bulk-focus') {
			failNextInstall = true;
			await page.click('[data-model="suggested"] input');
			await page.$eval('[data-selection-footer] button', (el) => el.focus());
			await page.keyboard.press('Enter');
			await page.waitForFunction(() =>
				document
					.querySelector('[data-model="suggested"]')
					.textContent.includes('409: busy right now')
			);
			await page.waitForFunction(
				() => !document.querySelector('[data-selection-footer] button').disabled
			);
			assert.equal(
				await page.evaluate(() => document.activeElement.matches('[data-selection-footer] button')),
				true,
				'failed bulk action restores the button'
			);
			return;
		}
		if (regression === 'refused-remove-focus') {
			refuseRemoval = true;
			page.once('dialog', (dialog) => dialog.accept());
			await page.$eval('[data-model="installed"] button', (el) => el.focus());
			await page.keyboard.press('Enter');
			await page.waitForFunction(() =>
				document.querySelector('[data-model="installed"]').textContent.includes('Remove: in_use')
			);
			await page.waitForFunction(
				() => !document.querySelector('[data-model="installed"] button:last-child').disabled
			);
			assert.equal(
				await page.evaluate(() =>
					document.activeElement.matches('[data-model="installed"] button:last-child')
				),
				true,
				'refused removal restores the button'
			);
			return;
		}
		if (regression === 'bulk-focus') {
			holdInstalls = true;
			await page.click('[data-model="suggested"] input');
			await page.$eval('[data-selection-footer] button', (el) => el.focus());
			await page.keyboard.press('Enter');
			await page.waitForFunction(() => !document.querySelector('[data-selection-footer]'));
			assert.equal(
				await page.evaluate(() =>
					document.activeElement.matches(
						'[data-model="suggested"] [data-heading], [data-model="suggested"] button'
					)
				),
				true,
				'focus returns to the model after the bulk button disappears'
			);
			return;
		}
		if (regression === 'wide-grid') {
			for (const [width, expected] of [
				[272, 1],
				[560, 2],
				[848, 3],
				[1424, 4],
				[2200, 4]
			]) {
				const columns = await page.$eval(
					'.model-grid',
					(el, width) => {
						el.style.width = `${width}px`;
						return getComputedStyle(el).gridTemplateColumns.split(' ').length;
					},
					width
				);
				assert.equal(columns, expected, `grid column count at ${width}px`);
			}
			return;
		}
		if (regression === 'footer-focus') {
			for (const viewport of [
				{ width: 390, height: 844 },
				{ width: 1024, height: 768 }
			]) {
				await page.setViewport(viewport);
				if (await page.$('[data-selection-footer]')) {
					await page.click('[data-model="suggested"] input');
					await page.waitForFunction(() => !document.querySelector('[data-selection-footer]'));
				}
				// Set up the overlap after responsive layout and font metrics have settled.
				await page.evaluate(async () => {
					await new Promise(requestAnimationFrame);
					await document.fonts.ready;
					await new Promise(requestAnimationFrame);
				});
				await page.$eval('[data-model="suggested"] input', (el) => {
					el.focus({ preventScroll: true });
					const scroller = el.closest('[data-model-scroller]');
					const bounds = scroller.getBoundingClientRect();
					scroller.scrollBy(0, el.getBoundingClientRect().bottom - bounds.bottom + 10);
				});
				assert.equal(
					await page.$eval('[data-model="suggested"] input', (el) => {
						const bounds = el.getBoundingClientRect();
						const scroller = el.closest('[data-model-scroller]').getBoundingClientRect();
						return bounds.bottom > scroller.bottom - 60 && bounds.bottom <= scroller.bottom;
					}),
					true,
					'checkbox starts in the future footer area'
				);
				await page.keyboard.press('Space');
				await page.waitForSelector('[data-selection-footer]');
				await new Promise((resolve) => setTimeout(resolve, 150));
				assert.equal(
					await page.evaluate(() => {
						const focus = document.activeElement.getBoundingClientRect();
						const footer = document
							.querySelector('[data-selection-footer]')
							.getBoundingClientRect();
						return (
							focus.bottom <= footer.top ||
							focus.top >= footer.bottom ||
							focus.right <= footer.left ||
							focus.left >= footer.right
						);
					}),
					true,
					`first selection stays visible at ${viewport.width}px`
				);
				const count = await page.evaluate(() => {
					// A closed Details section hides its links from Tab; its summary stays reachable.
					const controls = [
						...document.querySelectorAll(
							'[data-model] input:not(:disabled), [data-model] button:not(:disabled), [data-model] a[href], [data-model] summary'
						)
					].filter(
						(control) =>
							control.tagName === 'SUMMARY' || control.closest('details:not([open])') === null
					);
					controls.forEach((control, index) =>
						control.setAttribute('data-test-focus-index', String(index))
					);
					return controls.length;
				});
				assert.ok(count > 0);
				for (const reverse of [false, true]) {
					const indices = [...Array(count).keys()];
					if (reverse) indices.reverse();
					await page.$eval(`[data-test-focus-index="${indices[0]}"]`, (el) => el.focus());
					for (const [position, index] of indices.entries()) {
						if (position) {
							if (reverse) await page.keyboard.down('Shift');
							await page.keyboard.press('Tab');
							if (reverse) await page.keyboard.up('Shift');
						}
						const result = await page.evaluate(() => {
							const active = document.activeElement;
							const focus = active.getBoundingClientRect();
							const footer = document
								.querySelector('[data-selection-footer]')
								.getBoundingClientRect();
							return {
								index: active.getAttribute('data-test-focus-index'),
								visible:
									focus.bottom <= footer.top ||
									focus.top >= footer.bottom ||
									focus.right <= footer.left ||
									focus.left >= footer.right
							};
						});
						assert.equal(
							result.index,
							String(index),
							`visited every card control at ${viewport.width} in ${reverse ? 'reverse' : 'forward'} order`
						);
						assert.ok(
							result.visible,
							`control ${index} obscured at ${viewport.width} in ${reverse ? 'reverse' : 'forward'} order`
						);
					}
				}
			}
			return;
		}
		if (regression === 'gated-button') {
			const button = '[data-model="gated"] button';
			const details = await page.$eval(button, (el) => ({
				disabled: el.getAttribute('aria-disabled'),
				described: document.getElementById(el.getAttribute('aria-describedby'))?.textContent,
				opacity: getComputedStyle(el).opacity
			}));
			assert.equal(details.disabled, 'true');
			assert.match(details.described ?? '', /Hugging Face/);
			assert.ok(Number(details.opacity) < 1);
			await page.focus(button);
			await page.waitForResponse((response) => response.url().endsWith('/api/v1/admin/models'));
			await new Promise((done) => setTimeout(done, 100));
			assert.equal(await page.$eval(button, (el) => document.activeElement === el), true);
			const before = calls.filter((c) => c.endsWith('/install')).length;
			await page.click(button);
			assert.equal(calls.filter((c) => c.endsWith('/install')).length, before);
			return;
		}
		if (regression === 'footer-focus') {
			worker.models.unshift(...Array.from({ length: 12 }, (_, i) => model(`preceding-${i}`)));
		}
		if (regression === 'reserved') {
			await page.click('[data-model="suggested"] input');
			const disk = await page.$eval('[data-disk]', (el) => ({
				text: el.textContent,
				reserved: el.querySelector('[data-reserved]')?.getBoundingClientRect().width,
				track: el.querySelector('[role=meter]').getBoundingClientRect().width
			}));
			assert.match(disk.text, /Reserved: 6.00 GiB/);
			assert.match(disk.text, /After install: 5.00 GiB free/);
			assert.ok(Math.abs(disk.reserved / disk.track - 0.3) < 0.01);
			return;
		}
		if (regression === 'install-focus' || regression === 'preview-transition') {
			holdInstalls = true;
			if (regression === 'preview-transition') {
				await page.$eval('[data-model="suggested"] button', (el) => {
					el.dispatchEvent(new MouseEvent('mouseenter'));
					el.click();
				});
			} else {
				// Hold the inventory refresh that follows the install, so focus must
				// already be back on the row while that request is still open.
				delayNext = true;
				await page.$eval('[data-model="suggested"] button', (el) => el.focus());
				await page.keyboard.press('Enter');
			}
			await page.waitForFunction(() => !document.querySelector('[data-model="suggested"] input'));
			if (regression === 'install-focus') {
				assert.equal(
					await page.evaluate(() =>
						document.activeElement.matches(
							'[data-model="suggested"] [data-heading], [data-model="suggested"] button'
						)
					),
					true
				);
				releaseDelayed();
				return;
			}
			assert.doesNotMatch(
				await page.$eval('[data-disk]', (el) => el.textContent),
				/After install: 5.00 GiB free/
			);
			worker.models[0].download = {
				kind: 'install',
				state: 'downloading',
				bytes_done: 0,
				bytes_total: gib,
				error: null,
				op_id: op(opSequence)
			};
			await page.waitForSelector('[data-model="suggested"] progress');
			assert.doesNotMatch(
				await page.$eval('[data-disk]', (el) => el.textContent),
				/After install: 5.00 GiB free/
			);
			worker.models[0].installed = true;
			worker.models[0].download = { ...worker.models[0].download, state: 'done', bytes_done: gib };
			await page.waitForFunction(() =>
				document.querySelector('[data-model="suggested"]').textContent.includes('Installed')
			);
			await page.click('[data-model="large"] input');
			assert.match(
				await page.$eval('[data-disk]', (el) => el.textContent),
				/After install: 5.00 GiB free/
			);
			return;
		}
		if (!regression) {
			const meter = await page.$eval('[role="meter"]', (el) => ({
				now: el.getAttribute('aria-valuenow'),
				max: el.getAttribute('aria-valuemax'),
				text: el.getAttribute('aria-valuetext')
			}));
			assert.equal(Number(meter.now), 13 * gib);
			assert.equal(Number(meter.max), 20 * gib);
			assert.match(meter.text, /65%.*Used.*Free/);
			assert.match(
				await page.$eval('[data-model="suggested"]', (el) => el.textContent),
				/Suggested.*Not installed/s
			);
			assert.match(
				await page.$eval('[data-model="installed"] [data-slot="badge"]', (el) => el.textContent),
				/Installed/
			);
			assert.equal(await page.$('[data-selection-footer]'), null);
			await page.$eval('[data-model="suggested"] input', (el) => el.click());
			assert.ok(await page.$('[data-selection-footer]'));
			assert.match(
				await page.$eval('[data-disk]', (el) => el.textContent),
				/After install: 6.00 GiB free/
			);
			await page.$eval('[data-model="suggested"] input', (el) => el.click());
			assert.equal(await page.$('[data-selection-footer]'), null);
		}
		if (process.env.MODEL_SCREENSHOTS) {
			await page.$eval('[data-model="suggested"] button', (el) => el.focus());
			assert.match(
				await page.$eval('[data-disk]', (el) => el.textContent),
				/After install: 6.00 GiB free/
			);
			await page.$eval('[data-model="suggested"] button', (el) => el.blur());
			await page.click('[data-model="suggested"] input');
			for (const width of [1440, 1024, 390]) {
				await page.setViewport({ width, height: 900 });
				await page.screenshot({
					path: `${process.env.MODEL_SCREENSHOTS}-${width}.png`,
					fullPage: true
				});
			}
			await page.setViewport({ width: 1440, height: 900 });
			await page.evaluate(() => {
				const tokens = {
					background: '#ffffff',
					foreground: '#171717',
					card: '#ffffff',
					primary: '#171717',
					'primary-foreground': '#ffffff',
					secondary: '#eeeeee',
					'secondary-foreground': '#171717',
					muted: '#eeeeee',
					'muted-foreground': '#595959',
					accent: '#e9e9e9',
					'accent-foreground': '#171717',
					destructive: '#b42318',
					border: '#bdbdbd',
					input: '#888888',
					'status-success': '#146c2e',
					'status-running': '#175cd3',
					'status-warning': '#946200'
				};
				for (const [name, value] of Object.entries(tokens)) {
					for (const element of [
						document.documentElement,
						document.querySelector('[data-studio-shell]')
					]) {
						element.style.setProperty(`--${name}`, value);
						element.style.setProperty(`--color-${name}`, value);
					}
				}
			});
			await page.screenshot({
				path: `${process.env.MODEL_SCREENSHOTS}-light-1440.png`,
				fullPage: true
			});
			await page.click('[data-model="suggested"] input');
			await page.$eval('[data-model="suggested"] button', (el) => el.focus());
			await page.keyboard.press('Enter');
			await page.waitForSelector('[data-model="suggested"] progress');
			assert.match(await page.$eval('[data-model="suggested"]', (el) => el.textContent), /50%/);
			return;
		}
		if (!regression || regression === 'reconnect') {
			await page.click('[data-model="suggested"] input');
			await page.click('[data-model="large"] input');
			worker.models.push(
				model('polling', {
					download: {
						kind: 'install',
						state: 'downloading',
						bytes_done: 0,
						bytes_total: gib,
						error: null,
						op_id: op(0)
					}
				})
			);
			await page.waitForSelector('[data-model="polling"]', { timeout: 15000 });
			awaitingInventory = true;
			await page.waitForFunction(() => !document.querySelector('[data-model]'), { timeout: 4000 });
			awaitingInventory = false;
			await page.waitForSelector('[data-model="suggested"] input', { timeout: 15000 });
			assert.equal(await page.$eval('[data-model="suggested"] input', (el) => el.checked), true);
			assert.equal(await page.$eval('[data-model="large"] input', (el) => el.checked), true);
			if (regression) return;
			await page.click('[data-model="suggested"] input');
			await page.click('[data-model="large"] input');
			worker.models.pop();
		}
		if (!regression) {
			assert.equal(await page.$eval('[data-model="suggested"] input', (el) => el.checked), false);
			assert.match(
				await page.$eval('[data-model="suggested"]', (el) => el.textContent),
				/Suggested/i
			);
			worker.models[2].installed = false;
			worker.models.push(model('unfit-suggestion', { suggested: true, fits: false }));
			worker.models.push(model('gated-suggestion', { suggested: true, needs_token: true }));
			worker.models[1].download = {
				kind: 'install',
				state: 'downloading',
				bytes_done: 0,
				bytes_total: gib,
				error: null,
				op_id: op(0)
			};
			await page.reload({ waitUntil: 'domcontentloaded' });
			await page.waitForSelector('[data-model="suggested"] input');
			assert.equal(await page.$eval('[data-model="suggested"] input', (el) => el.checked), true);
			assert.equal(
				await page.$eval('[data-model="unfit-suggestion"] input', (el) => el.checked),
				false
			);
			assert.equal(await page.$('[data-model="gated-suggestion"] input:not(:disabled)'), null);
			await page.click('[data-model="suggested"] input');
			const beforePoll = calls.filter((c) => c === 'GET /api/v1/admin/models').length;
			await new Promise((resolve) => setTimeout(resolve, 1500));
			assert.ok(calls.filter((c) => c === 'GET /api/v1/admin/models').length > beforePoll);
			assert.equal(await page.$eval('[data-model="suggested"] input', (el) => el.checked), false);
			worker.models.splice(4);
			worker.models[1].download = null;
			worker.models[2].installed = true;
			await page.reload({ waitUntil: 'domcontentloaded' });
			await page.waitForSelector('[data-model="suggested"] input');
		}
		if (!regression || regression === 'queued-cancel' || regression === 'cancelled-status') {
			if (regression !== 'cancelled-status') {
				worker.models[1].download = {
					kind: 'cancel',
					state: 'queued',
					bytes_done: 0,
					bytes_total: gib,
					error: null,
					op_id: op(0)
				};
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.waitForSelector('[data-model="large"]');
				assert.match(
					await page.$eval('[data-model="large"] [role=status]', (el) => el.textContent),
					/Waiting for the worker/
				);
				assert.equal(await page.$('[data-model="large"] button'), null);
			}
			if (regression !== 'queued-cancel') {
				worker.models[1].download = {
					kind: 'cancel',
					state: 'failed',
					bytes_done: 0,
					bytes_total: gib,
					error: 'cancelled',
					op_id: op(0)
				};
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.waitForSelector('[data-model="large"]');
				assert.match(
					await page.$eval('[data-model="large"] [role=status]', (el) => el.textContent),
					/Download cancelled/
				);
				assert.equal(await page.$('[data-model="large"] [role=alert]'), null);
			}
			worker.models[1].download = null;
			if (regression) return;
			await page.reload({ waitUntil: 'domcontentloaded' });
			await page.waitForSelector('[data-model="suggested"] input');
		}
		if (regression) {
			if (regression === 'remount-remove') {
				await page.reload({ waitUntil: 'domcontentloaded' });
				await page.waitForSelector('[data-model="installed"]');
				assert.equal(await page.$('[data-model="installed"] progress'), null);
				assert.doesNotMatch(
					await page.$eval('[data-model="installed"]', (el) => el.textContent),
					/Cancel/
				);
			} else if (regression === 'retry-remove') {
				assert.match(
					await page.$eval('[data-model="installed"]', (el) => el.textContent),
					/Remove/
				);
				assert.ok(await page.$('[data-model="installed"] button'));
			} else if (regression === 'later-active' || regression === 'later-failed') {
				page.once('dialog', (dialog) => dialog.accept());
				await page.$eval('[data-model="installed"] button', (el) => el.click());
				await page.waitForFunction(() =>
					document
						.querySelector('[data-model="installed"]')
						.textContent.includes('Waiting for the worker')
				);
				worker.models[2].installed = false;
				worker.models[2].download = {
					kind: 'install',
					state: regression === 'later-active' ? 'downloading' : 'failed',
					bytes_done: gib / 2,
					bytes_total: gib,
					error: regression === 'later-failed' ? 'download_failed' : null,
					op_id: op(++opSequence)
				};
				if (regression === 'later-active') {
					await page.waitForSelector('[data-model="installed"] progress', { timeout: 4000 });
					assert.match(
						await page.$eval('[data-model="installed"]', (el) => el.textContent),
						/Cancel/
					);
				} else {
					await page.waitForFunction(
						() =>
							document
								.querySelector('[data-model="installed"]')
								.textContent.includes('download_failed'),
						{ timeout: 4000 }
					);
					assert.doesNotMatch(
						await page.$eval('[data-model="installed"]', (el) => el.textContent),
						/Remove: download_failed/
					);
				}
			} else if (regression === 'removal') {
				page.once('dialog', (dialog) => dialog.accept());
				await page.$eval('[data-model="installed"] button', (el) => el.click());
				await page.waitForFunction(
					() =>
						document
							.querySelector('[data-model="installed"]')
							.textContent.includes('Waiting for the worker') ||
						document.querySelector('[data-model="installed"] progress')
				);
				assert.equal(await page.$('[data-model="installed"] progress'), null);
				assert.doesNotMatch(
					await page.$eval('[data-model="installed"]', (el) => el.textContent),
					/Cancel/
				);
			} else {
				await page.click('[data-model="suggested"] input');
				await page.evaluate(() =>
					[...document.querySelectorAll('button')]
						.find((b) => /Download selected|Descargar selección/.test(b.textContent))
						.click()
				);
				if (regression === 'spanish') {
					await page.waitForSelector('[data-model="suggested"] progress');
					assert.match(
						await page.$eval('[data-model="suggested"] progress', (el) =>
							el.getAttribute('aria-label')
						),
						/progreso de descarga/i
					);
					assert.match(
						await page.$eval('[aria-live="polite"].sr-only', (el) => el.textContent),
						/iniciada/i
					);
				} else {
					await page.waitForFunction(() =>
						document
							.querySelector('[data-model="suggested"]')
							.textContent.includes('Waiting for the worker')
					);
					if (regression === 'failure')
						assert.equal(await page.$('[data-model="suggested"] [role="alert"]'), null);
					else {
						await page.click('[data-model="large"] input');
						assert.equal(
							await page.evaluate(
								() =>
									[...document.querySelectorAll('button')].find((b) =>
										b.textContent.includes('Download selected')
									).disabled
							),
							false
						);
					}
				}
			}
			return;
		}

		const second = await browser.newPage();
		recordPageEvents(second);
		await second.evaluateOnNewDocument(() => localStorage.setItem('potocolom-tour-done', '1'));
		await second.goto(`http://127.0.0.1:${server.address().port}/app?view=admin&tab=models`);
		await second.waitForSelector('[data-model="suggested"] input');
		holdInstalls = true;
		worker.models[0].download = {
			kind: 'install',
			state: 'done',
			bytes_done: 0,
			bytes_total: gib,
			error: null,
			op_id: op(0)
		};
		for (const view of [page, second]) {
			await view.bringToFront();
			await view.click('[data-model="suggested"] input');
			await view.evaluate(() =>
				[...document.querySelectorAll('button')]
					.find((b) => b.textContent.includes('Download selected'))
					.click()
			);
			await view.waitForFunction(() =>
				document
					.querySelector('[data-model="suggested"]')
					.textContent.includes('Waiting for the worker')
			);
		}
		await new Promise((done) => setTimeout(done, 1200));
		for (const view of [page, second])
			assert.match(
				await view.$eval('[data-model="suggested"]', (el) => el.textContent),
				/Waiting for the worker/,
				'older record must not settle a newer operation'
			);
		worker.models[0].download = {
			kind: 'install',
			state: 'done',
			bytes_done: gib,
			bytes_total: gib,
			error: null,
			op_id: op(opSequence)
		};
		for (const view of [page, second]) {
			await view.bringToFront();
			await view.waitForSelector('[data-model="suggested"] input', { timeout: 4000 });
		}
		await second.close();
		await page.bringToFront();
		holdInstalls = false;
		worker.models[0].download = null;
		installs = 0;
		cancelNotRunning = true;
		silentInstall = true;
		await page.click('[data-model="suggested"] input');
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((b) => b.textContent.includes('Download selected'))
				.click()
		);
		await page.waitForFunction(() =>
			document
				.querySelector('[data-model="suggested"]')
				.textContent.includes('Waiting for the worker')
		);
		await page.$eval('[data-model="suggested"] button', (el) => el.click());
		await page.waitForSelector('[data-model="suggested"] input');
		assert.match(
			await page.$eval('[data-model="suggested"] [role=status]', (el) => el.textContent),
			/No download running/
		);
		assert.equal(await page.$('[data-model="suggested"] [role=alert]'), null);
		cancelNotRunning = false;
		worker.models[0].download = null;
		installs = 0;
		assert.equal(await page.$eval('[data-model="suggested"] input', (el) => el.checked), false);
		await page.click('[data-model="suggested"] input');

		assert.match(
			await page.$eval('[data-model="large"]', (el) => el.textContent),
			/Needs 16.00 GiB VRAM/
		);
		assert.equal(await page.$eval('[data-model="gated"] input', (el) => el.disabled), true);
		await page.click('[data-model="large"] input');
		assert.equal(
			await page.evaluate(
				() =>
					[...document.querySelectorAll('button')].find((b) =>
						b.textContent.includes('Download selected')
					).disabled
			),
			true
		);
		await page.click('[data-model="large"] input');
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((b) => b.textContent.includes('Download selected'))
				.click()
		);
		await page.waitForSelector('[data-model="suggested"] progress');
		assert.match(
			await page.$eval('[data-model="suggested"]', (el) => el.textContent),
			/0.50 GiB \/ 1.00 GiB/
		);
		delayNext = true;
		const pollCount = calls.filter((c) => c === 'GET /api/v1/admin/models').length;
		await new Promise((done) => setTimeout(done, 1200));
		assert.ok(calls.filter((c) => c === 'GET /api/v1/admin/models').length > pollCount);
		assert.ok(releaseDelayed);
		await page.evaluate(() =>
			[...document.querySelector('[data-model="suggested"]').querySelectorAll('button')]
				.find((b) => b.textContent.includes('Cancel'))
				.click()
		);
		await page.waitForFunction(() =>
			document.querySelector('[data-model="suggested"]').textContent.includes('cancelled')
		);
		releaseDelayed();
		await new Promise((done) => setTimeout(done, 300));
		assert.equal(await page.$('[data-model="suggested"] progress'), null);
		const stoppedCount = calls.filter((c) => c === 'GET /api/v1/admin/models').length;
		await new Promise((done) => setTimeout(done, 1200));
		assert.equal(calls.filter((c) => c === 'GET /api/v1/admin/models').length, stoppedCount);
		await page.click('[data-model="suggested"] input');
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((b) => b.textContent.includes('Download selected'))
				.click()
		);
		await page.waitForFunction(() => !document.querySelector('[data-model="suggested"] input'));
		await new Promise((done) => setTimeout(done, 300));
		await page.evaluate(() => {
			const now = Date.now();
			Date.now = () => now + 60000;
		});
		await page.click('[data-model="large"] input');
		assert.equal(
			await page.evaluate(
				() =>
					[...document.querySelectorAll('button')].find((b) =>
						b.textContent.includes('Download selected')
					).disabled
			),
			true
		);
		await new Promise((done) => setTimeout(done, 1100));
		assert.equal(await page.$('[data-model="suggested"] input'), null);
		await page.click('[data-model="large"] input');
		await page.waitForSelector('[data-model="suggested"] progress', { timeout: 9000 });
		assert.equal(retryPolls, 0);
		assert.match(
			await page.$eval('[data-model="suggested"] progress', (el) => el.getAttribute('aria-label')),
			/suggested/
		);
		await page.$eval('[data-model="suggested"] button', (el) => el.focus());
		await page.$eval('[data-model="suggested"] button', (el) => el.click());
		await page.waitForSelector('[data-model="suggested"] input');
		assert.equal(
			await page.evaluate(() =>
				document.querySelector('[data-model-list]').contains(document.activeElement)
			),
			true
		);
		silentInstall = true;
		await page.waitForSelector('[data-model="suggested"] input');
		await page.click('[data-model="suggested"] input');
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((b) => b.textContent.includes('Download selected'))
				.click()
		);
		await page.waitForFunction(() =>
			document
				.querySelector('[data-model="suggested"]')
				.textContent.includes('Waiting for the worker')
		);
		await page.$eval('[data-model="suggested"] button', (el) => el.click());
		await page.waitForSelector('[data-model="suggested"] input');
		assert.doesNotMatch(
			await page.$eval('[data-model="suggested"]', (el) => el.textContent),
			/Waiting for the worker/
		);

		refuseRemoval = true;
		for (let attempt = 0; attempt < 2; attempt++) {
			page.once('dialog', (dialog) => dialog.accept());
			await page.$eval('[data-model="installed"] button', (el) => el.click());
			await page.waitForFunction(() =>
				document.querySelector('[data-model="installed"]').textContent.includes('Remove: in_use')
			);
			await page.waitForFunction(
				() => !document.querySelector('[data-model="installed"] button:last-child').disabled
			);
			await page.$eval('[data-model="installed"] [role=alert] button', (el) => el.click());
		}
		refuseRemoval = false;
		page.once('dialog', (dialog) => {
			assert.doesNotMatch(dialog.message(), /GiB/);
			dialog.accept();
		});
		await page.$eval('[data-model="installed"] button', (el) => el.click());
		await page.waitForFunction(
			() => document.querySelector('[data-model="installed"] button')?.disabled
		);
		assert.ok(removalPolls > 0);
		await page.evaluate(() => {
			const now = Date.now();
			Date.now = () => now + 60000;
		});
		await new Promise((done) => setTimeout(done, 1100));
		assert.equal(await page.$eval('[data-model="installed"] button', (el) => el.disabled), true);
		await page.waitForSelector('[data-model="installed"] input');
		assert.match(
			await page.$eval('[aria-live="polite"].sr-only', (el) => el.textContent),
			/installed removed/
		);
		assert.equal(removalPolls, 0);
		assert.ok(
			calls.some((c) => c.endsWith('/install')) &&
				calls.some((c) => c.endsWith('/cancel')) &&
				calls.some((c) => c.startsWith('DELETE '))
		);
		failNextInstall = true;
		await page.click('[data-model="suggested"] input');
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((b) => b.textContent.includes('Download selected'))
				.click()
		);
		await page.waitForFunction(() =>
			document.querySelector('[data-model="suggested"]').textContent.includes('409: busy right now')
		);
		const failedPoll = calls.filter((c) => c === 'GET /api/v1/admin/models').length;
		await new Promise((done) => setTimeout(done, 10500));
		assert.ok(calls.filter((c) => c === 'GET /api/v1/admin/models').length > failedPoll);
		assert.match(
			await page.$eval('[data-model="suggested"]', (el) => el.textContent),
			/409: busy right now/
		);
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((b) => b.textContent.includes('Download selected'))
				.click()
		);
		await page.waitForFunction(() => !document.querySelector('[data-model="suggested"] input'));
		workerGone = true;
		await page.waitForFunction(() => !document.querySelector('[data-worker]'));
		const gonePoll = calls.filter((c) => c === 'GET /api/v1/admin/models').length;
		await new Promise((done) => setTimeout(done, 1500));
		assert.equal(calls.filter((c) => c === 'GET /api/v1/admin/models').length, gonePoll);
		workerGone = false;
		await page.close();
		role = 'viewer';
		const before = calls.filter((c) => c === 'GET /api/v1/admin/models').length;
		const viewer = await browser.newPage();
		recordPageEvents(viewer);
		await viewer.evaluateOnNewDocument(() => localStorage.setItem('potocolom-tour-done', '1'));
		await viewer.goto(`http://127.0.0.1:${server.address().port}/app?view=admin&tab=models`, {
			waitUntil: 'networkidle0'
		});
		assert.equal(await viewer.$('[data-model]'), null);
		assert.equal(calls.filter((c) => c === 'GET /api/v1/admin/models').length, before);
		role = 'user';
		const user = await browser.newPage();
		recordPageEvents(user);
		await user.goto(`http://127.0.0.1:${server.address().port}/app?view=admin&tab=models`, {
			waitUntil: 'networkidle0'
		});
		assert.equal(await user.$('[data-model]'), null);
		assert.equal(calls.filter((c) => c === 'GET /api/v1/admin/models').length, before);
		await user.close();
		localMode = true;
		const local = await browser.newPage();
		recordPageEvents(local);
		await local.evaluateOnNewDocument(() => localStorage.setItem('potocolom-tour-done', '1'));
		await local.goto(`http://127.0.0.1:${server.address().port}/app?view=admin&tab=models`, {
			waitUntil: 'domcontentloaded'
		});
		await local.waitForSelector('[data-model="suggested"]');
		assert.equal(
			await local.evaluate(() =>
				[...document.querySelectorAll('button')].some((b) =>
					/Invitations|Invite someone/.test(b.textContent)
				)
			),
			false
		);
		for (const tab of ['audit', 'banner', 'users', 'invitations']) {
			await local.goto(`http://127.0.0.1:${server.address().port}/app?view=admin&tab=${tab}`);
			await local.waitForFunction(
				() => new URL(location.href).searchParams.get('tab') === 'models'
			);
		}
		// The account view in local mode is the turn-on-accounts card (#795).
		await local.goto(`http://127.0.0.1:${server.address().port}/app?view=account`);
		await local.waitForSelector('#turn-on-email');
		assert.equal(new URL(local.url()).searchParams.get('view'), 'account');
		await local.goto(`http://127.0.0.1:${server.address().port}/app`);
		// The sidebar renders after the account probe, so wait before clicking.
		await local.waitForSelector('[data-testid="admin-nav-entry"]', { visible: true });
		await local.click('[data-testid="admin-nav-entry"]');
		await local.waitForSelector('[data-model="suggested"]');
		await local.evaluate(() =>
			[...document.querySelectorAll('nav')]
				.find((nav) =>
					[...nav.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Fleet')
				)
				.querySelectorAll('button')
				.values()
				.find((b) => b.textContent.trim() === 'Fleet')
				.click()
		);
		await local.waitForFunction(() =>
			[...document.querySelectorAll('nav button[aria-current=page]')].some(
				(b) => b.textContent.trim() === 'Fleet'
			)
		);
		await local.evaluate(() =>
			[...document.querySelectorAll('nav')]
				.find((nav) =>
					[...nav.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Fleet')
				)
				.querySelectorAll('button')
				.values()
				.find((b) => b.textContent.trim() === 'Models')
				.click()
		);
		await local.waitForSelector('[data-model="suggested"]');
		assert.equal(
			await local.evaluate(() =>
				[...document.querySelectorAll('button')].some((b) =>
					/^(Audit|Banner|Users|Invitations)$/.test(b.textContent.trim())
				)
			),
			false
		);
	} finally {
		if (releaseDelayed) releaseDelayed();
		if (browser) await browser.close();
		await new Promise((done) => server.close(done));
	}
});
