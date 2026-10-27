import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { access, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { basename, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import puppeteer from 'puppeteer-core';

const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
// A ceiling, never a delay: every use waits on a condition or polls for one.
// The self-hosted runners share one machine with other work, and 5 s failed
// under load on checks that pass on rerun (issue #670).
const WAIT_MS = 20000;
// The frame header this build writes on the canvas socket: one kind byte, the
// 16 byte session id and a 4 byte big endian revision, then the image.
const FRAME_HEADER_BYTES = 21;
const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));
async function waitFixture(promise, label) {
	let timeout;
	try {
		await Promise.race([
			promise,
			new Promise((_, reject) => {
				timeout = setTimeout(() => reject(new Error(label)), WAIT_MS);
			})
		]);
	} finally {
		clearTimeout(timeout);
	}
}
const MODEL = {
	id: 'history-smoke',
	name: 'History smoke model',
	capabilities: ['realtime', 'text_to_image'],
	min_vram_gb: 0,
	default: true,
	realtime_p95_ms: 100,
	estimated_gpu_ms_default: 100,
	parameters: {
		type: 'object',
		required: ['prompt'],
		properties: {
			prompt: { type: 'string' },
			steps: { type: 'integer', minimum: 1, maximum: 4, default: 1 },
			structure_strength: { type: 'number', minimum: 0, maximum: 1, default: 0.7 },
			// The panel mounts its selection controls only for a model whose
			// manifest declares this, so the lasso tests need it declared.
			mask: { type: 'string' }
		}
	}
};

const CONTENT_TYPES = {
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

// The answers a caller did not override, so every existing caller keeps them.
function defaultApiBody(path) {
	if (path === '/api/v1/models') return [MODEL];
	if (path === '/api/v1/generations/models') return { models: [MODEL] };
	if (/^\/api\/v1\/generations\/[^/]+\/lineage$/.test(path)) {
		return { ancestors: [], children: [], descendant_count: 0, descendants_truncated: false };
	}
	if (path === '/api/v1/metrics/gpu/history') {
		return {
			from: '1970-01-01T00:00:00Z',
			to: '1970-01-01T00:05:00Z',
			rollup: 'raw',
			samples: [],
			truncated: false
		};
	}
	if (path === '/api/v1/config')
		return { auth_methods: [], billing_enabled: false, languages: ['en', 'es'] };
	return [];
}

// Per-path API fixtures: `{ body, delayMs? }`. A key is the full
// `pathname + search`, a `pathname + '?' + ...` prefix such as
// `/api/v1/generations?starred=true`, or the plain pathname; the more specific
// key wins, so the starred list can answer differently from the history list.
// `delayMs` holds the response back, which is how a test loads history before
// starred items arrive. Paths without an answer keep today's default answers.
async function serveBuild(apiAnswers = {}) {
	const server = createServer(async (request, response) => {
		try {
			const url = new URL(request.url, 'http://localhost');
			const path = url.pathname;
			if (path.startsWith('/api/')) {
				const key = `${path}${url.search}`;
				const answer =
					apiAnswers[key] ??
					Object.entries(apiAnswers)
						.filter(([queryKey]) => queryKey.includes('?') && key.startsWith(queryKey))
						.sort(([left], [right]) => right.length - left.length)[0]?.[1] ??
					apiAnswers[path] ??
					(path === '/api/v1/account'
						? { status: 404, body: null }
						: { body: defaultApiBody(path) });
				if (answer.delayMs) await pause(answer.delayMs);
				const body =
					typeof answer.body === 'function' ? await answer.body(url, request) : answer.body;
				const status =
					typeof answer.status === 'function'
						? await answer.status(url, request)
						: (answer.status ?? 200);
				response.writeHead(status, { 'Content-Type': 'application/json' });
				if (status === 204 || status === 304) response.end();
				else response.end(body === null ? '' : JSON.stringify(body));
				return;
			}
			const clean = resolve(build, '.' + decodeURIComponent(path));
			if (clean !== build && !clean.startsWith(build + '/')) {
				response.writeHead(404).end();
				return;
			}
			let found;
			for (const candidate of [
				clean,
				clean + '.html',
				join(clean, 'index.html'),
				join(build, '200.html'),
				join(build, 'index.html')
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
				'Content-Type': CONTENT_TYPES[extname(found)] ?? 'application/octet-stream'
			});
			response.end(await readFile(found));
		} catch (error) {
			response.writeHead(500).end(String(error));
		}
	});
	await new Promise((resolveServer) => server.listen(0, '127.0.0.1', resolveServer));
	return server;
}

function viewPrefsStorageKey(owner, section) {
	const profile = owner.kind === 'local' ? 'local' : `account-${owner.id.toLowerCase()}`;
	return `potocolom-view-v1:${profile}:${section}`;
}

async function readViewPrefs(page, owner, section) {
	return page.evaluate(
		({ profileKey, selectedSection }) => {
			const raw = localStorage.getItem(`potocolom-view-v1:${profileKey}:${selectedSection}`);
			return raw === null ? null : JSON.parse(raw);
		},
		{
			profileKey: owner.kind === 'local' ? 'local' : `account-${owner.id.toLowerCase()}`,
			selectedSection: section
		}
	);
}

async function chromeExecutable() {
	const candidates = [
		process.env.PUPPETEER_EXECUTABLE_PATH,
		process.env.CHROME_PATH,
		process.env.CHROME_BIN,
		'/usr/bin/google-chrome',
		'/usr/bin/chromium',
		'/usr/bin/chromium-browser'
	].filter(Boolean);
	for (const candidate of candidates) {
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

function installSocketAdapter(page) {
	return page.evaluateOnNewDocument(() => {
		window.__historySockets = [];
		class HistorySocket {
			static CONNECTING = 0;
			static OPEN = 1;
			static CLOSING = 2;
			static CLOSED = 3;
			readyState = 0;
			bufferedAmount = 0;
			binaryType = 'arraybuffer';
			onopen = null;
			onmessage = null;
			onerror = null;
			onclose = null;
			sent = [];
			frames = [];
			sessionId = '12345678-1234-4123-8123-123456789abc';

			constructor(url) {
				window.__historySockets.push(this);
				this.url = String(url);
				setTimeout(() => {
					this.readyState = HistorySocket.OPEN;
					this.onopen?.(new Event('open'));
				}, 0);
			}

			send(data) {
				if (typeof data === 'string') {
					const message = JSON.parse(data);
					this.sent.push(message);
					if (message.type === 'open') {
						queueMicrotask(() =>
							this.onmessage?.(
								new MessageEvent('message', {
									data: JSON.stringify({ type: 'ready', session_id: this.sessionId })
								})
							)
						);
					}
					if (message.type === 'update_params') {
						queueMicrotask(() =>
							this.onmessage?.(
								new MessageEvent('message', {
									data: JSON.stringify({ type: 'params_updated', params: message.params })
								})
							)
						);
					}
					return;
				}
				const bytes =
					data instanceof ArrayBuffer
						? new Uint8Array(data)
						: new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
				this.frames.push(bytes.slice());
				const output = bytes.slice();
				output[0] = 2;
				queueMicrotask(() =>
					this.onmessage?.(new MessageEvent('message', { data: output.buffer }))
				);
			}

			close(code = 1000) {
				this.readyState = HistorySocket.CLOSING;
				queueMicrotask(() => {
					this.readyState = HistorySocket.CLOSED;
					this.onclose?.(new CloseEvent('close', { code }));
				});
			}
		}
		window.WebSocket = HistorySocket;
	});
}

// The shared browser setup behind openCanvas and openStudio: a static server
// over the build, a fresh Chrome, and one page on /app with the locale, the
// socket stub and the caller's setup installed before the document loads.
// `verifyErrors` stays on for the tests and is switched off only where a failed
// open would report the page error instead of its own cause.
async function launchStudio(
	locale,
	setup,
	apiAnswers,
	waitUntil,
	appPath = '/app',
	setupArgs = []
) {
	const server = await serveBuild(apiAnswers);
	let browser;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-gpu']
		});
		const page = await browser.newPage();
		page.setDefaultTimeout(WAIT_MS);
		const errors = [];
		page.on('pageerror', (error) => errors.push(error.stack ?? error.message));
		await page.setViewport({ width: 1440, height: 1100 });
		await page.evaluateOnNewDocument((selectedLocale) => {
			try {
				localStorage.setItem('locale', selectedLocale);
			} catch {
				// The initial blank document can have an opaque origin.
			}
		}, locale);
		await installSocketAdapter(page);
		if (setup) await page.evaluateOnNewDocument(setup, ...setupArgs);
		await page.goto(`http://127.0.0.1:${server.address().port}${appPath}`, { waitUntil });
		return {
			page,
			browser,
			server,
			async close(verifyErrors = true) {
				await browser.close();
				await new Promise((resolveServer) => server.close(resolveServer));
				if (verifyErrors) assert.deepEqual(errors, [], 'no uncaught browser errors');
			}
		};
	} catch (error) {
		if (browser) await browser.close();
		await new Promise((resolveServer) => server.close(resolveServer));
		throw error;
	}
}

async function openCanvas(
	locale = 'en',
	setup,
	{ apiAnswers = {}, waitUntil = 'networkidle0' } = {}
) {
	const harness = await launchStudio(locale, setup, apiAnswers, waitUntil);
	try {
		const { page } = harness;
		const tab = locale === 'es' ? 'Lienzo en tiempo real' : 'Realtime canvas';
		await page.waitForFunction(
			(name) =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === name
				),
			{ timeout: WAIT_MS, polling: 'mutation' },
			tab
		);
		await page.evaluate(
			(name) =>
				[...document.querySelectorAll('button')]
					.find((button) => button.textContent?.trim() === name)
					?.click(),
			tab
		);
		await page.waitForSelector('canvas[aria-label]', { timeout: WAIT_MS });
		await page.waitForFunction(
			() => {
				const canvas = document.querySelector('canvas[aria-label]');
				if (!canvas) return false;
				const pixel = canvas.getContext('2d')?.getImageData(256, 256, 1, 1).data;
				return pixel?.[0] === 255 && pixel[1] === 255 && pixel[2] === 255 && pixel[3] === 255;
			},
			{ timeout: WAIT_MS }
		);
		return harness;
	} catch (error) {
		await harness.close(false);
		throw error;
	}
}

// Opens /app on the generate view with per-path API fixtures and waits for the
// history strip thumbnails, so a keyboard test never has to visit the canvas.
async function openStudio(apiAnswers = {}) {
	const harness = await launchStudio('en', undefined, apiAnswers, 'domcontentloaded');
	try {
		await harness.page.waitForFunction(
			() => document.querySelectorAll('button[data-strip-thumb]').length > 0,
			{ timeout: WAIT_MS }
		);
		return harness;
	} catch (error) {
		await harness.close(false);
		throw error;
	}
}

test('an owner recheck restarts history and stream updates without accepting late history', async () => {
	const ownerId = '550e8400-e29b-41d4-a716-446655440040';
	const oldJob = '550e8400-e29b-41d4-a716-446655440041';
	const newJob = '550e8400-e29b-41d4-a716-446655440042';
	const accountAdmin = { id: ownerId, email: 'owner@example.com', role: 'admin' };
	const accountUser = { id: ownerId, email: 'owner@example.com', role: 'user' };
	const queued = (id, prompt) => ({
		id,
		model_id: MODEL.id,
		source_asset_id: null,
		params: { prompt },
		state: 'queued',
		progress: 0,
		gpu_ms: null,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: '2026-10-27T00:00:00Z',
		dispatched_at: null,
		finished_at: null,
		starred_at: null,
		expired_favorite: false,
		assets: []
	});
	let accountReads = 0;
	let historyReads = 0;
	let announceOldHistory;
	let announceNewHistory;
	let announceRecheck;
	let releaseAdminFailure;
	let announceAdminRequest;
	const oldHistoryStarted = new Promise((resolve) => (announceOldHistory = resolve));
	const newHistoryStarted = new Promise((resolve) => (announceNewHistory = resolve));
	const recheckStarted = new Promise((resolve) => (announceRecheck = resolve));
	const adminRequestStarted = new Promise((resolve) => (announceAdminRequest = resolve));
	const heldAdminFailure = new Promise((resolve) => (releaseAdminFailure = resolve));
	const answers = {
		'/api/v1/account': {
			body: () => {
				accountReads += 1;
				if (accountReads === 2) announceRecheck();
				return accountReads === 1 ? accountAdmin : accountUser;
			}
		},
		'/api/v1/users': {
			status: 403,
			body: () => {
				announceAdminRequest();
				return heldAdminFailure;
			}
		},
		'/api/v1/generations?limit=50': {
			body: () => {
				historyReads += 1;
				if (historyReads === 1) {
					announceOldHistory();
					return [queued(oldJob, 'owner-a queued job')];
				}
				if (historyReads === 2) {
					announceNewHistory();
					return [queued(newJob, 'owner-b queued job')];
				}
				return [];
			}
		},
		'/api/v1/generations?starred=true&limit=200': { body: [] }
	};
	const harness = await launchStudio(
		'en',
		() => {
			window.__ownerEventSources = [];
			const originalFetch = window.fetch.bind(window);
			let historyResponses = 0;
			window.__releaseOldHistoryJson = null;
			window.fetch = async (input, ...rest) => {
				const url = String(input);
				const response = await originalFetch(input, ...rest);
				if (url === '/api/v1/generations?limit=50' && ++historyResponses === 1) {
					const body = await response.text();
					const buffered = new Response(body, {
						status: response.status,
						statusText: response.statusText,
						headers: response.headers
					});
					const readJson = buffered.json.bind(buffered);
					buffered.json = () =>
						new Promise((resolve, reject) => {
							window.__releaseOldHistoryJson = () => readJson().then(resolve, reject);
						});
					return buffered;
				}
				return response;
			};
			window.EventSource = class {
				constructor(url) {
					this.url = String(url);
					this.closed = false;
					window.__ownerEventSources.push(this);
				}
				close() {
					this.closed = true;
				}
			};
		},
		answers,
		'domcontentloaded',
		'/app?view=admin&tab=users'
	);
	try {
		const { page } = harness;
		await waitFixture(oldHistoryStarted, 'owner A history request did not start');
		await waitFixture(adminRequestStarted, 'the admin endpoint did not receive the role check');
		await page.waitForFunction(() => typeof window.__releaseOldHistoryJson === 'function', {
			timeout: WAIT_MS
		});
		releaseAdminFailure({ detail: 'role changed' });
		await waitFixture(recheckStarted, 'the admin 403 did not recheck the account');
		await waitFixture(newHistoryStarted, 'the changed owner epoch did not refetch history');
		await page.waitForFunction(
			(id) =>
				window.__ownerEventSources.some(
					(source) => source.url === `/api/v1/generations/${id}/events`
				),
			{ timeout: WAIT_MS },
			newJob
		);
		assert.equal(accountReads, 2, 'the admin 403 caused one account role recheck');
		assert.equal(historyReads, 2, 'the new owner epoch fetched history while the old body waited');
		const streamBeforeLateResponse = await page.evaluate(
			(id) =>
				window.__ownerEventSources.find(
					(source) => source.url === `/api/v1/generations/${id}/events`
				)?.closed,
			newJob
		);
		assert.equal(streamBeforeLateResponse, false, 'the new queued job has an open event stream');

		await page.evaluate(() => window.__releaseOldHistoryJson());
		await pause(1700);
		const streams = await page.evaluate(() =>
			window.__ownerEventSources.map(({ url, closed }) => ({ url, closed }))
		);
		assert.ok(
			streams.some(
				(source) => source.url === `/api/v1/generations/${newJob}/events` && !source.closed
			),
			'a late response for the old owner cannot stop the current owner stream'
		);
		assert.equal(
			streams.some((source) => source.url === `/api/v1/generations/${oldJob}/events`),
			false,
			'a late old-owner history cannot start an old generation stream'
		);
	} finally {
		releaseAdminFailure({ detail: 'role changed' });
		await harness.page.evaluate(() => window.__releaseOldHistoryJson?.()).catch(() => {});
		await harness.close();
	}
});

test('account 404 plus empty auth methods uses local preferences without reading account profiles', async () => {
	const accountA = { kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' };
	const seeded = { version: 1, section: 'metrics', value: { tab: 'benchmarks' } };
	const harness = await launchStudio(
		'en',
		(seededPreferences) => {
			localStorage.setItem(
				'potocolom-view-v1:account-550e8400-e29b-41d4-a716-446655440000:metrics',
				JSON.stringify(seededPreferences)
			);
		},
		{},
		'domcontentloaded',
		'/app',
		[seeded]
	);
	try {
		const { page } = harness;
		await page.waitForFunction(() =>
			document.querySelector('[data-studio-shell]')?.textContent?.includes('Generate')
		);
		assert.equal(await readViewPrefs(page, { kind: 'local' }, 'metrics'), null);
		assert.deepEqual(await readViewPrefs(page, accountA, 'metrics'), seeded);
		assert.notEqual(
			viewPrefsStorageKey({ kind: 'local' }, 'metrics'),
			viewPrefsStorageKey(accountA, 'metrics')
		);
	} finally {
		await harness.close();
	}
});

async function bitmap(page) {
	return page.evaluate(() => {
		const canvas = document.querySelector('canvas[aria-label="Drawing surface"]');
		if (!canvas) throw new Error('drawing canvas not found');
		const data = canvas.getContext('2d').getImageData(0, 0, 512, 512).data;
		let hash = 2166136261;
		for (const value of data) {
			hash ^= value;
			hash = Math.imul(hash, 16777619) >>> 0;
		}
		return { hash, center: [...data.slice((256 * 512 + 256) * 4, (256 * 512 + 256) * 4 + 4)] };
	});
}

async function surfacePixel(page, x, y) {
	return page.$eval(
		'canvas[aria-label="Drawing surface"]',
		(canvas, pixelX, pixelY) => [
			...canvas.getContext('2d').getImageData(pixelX, pixelY, 1, 1).data
		],
		x,
		y
	);
}

async function waitForBitmap(page, expectedHash) {
	const deadline = Date.now() + WAIT_MS;
	while (Date.now() < deadline) {
		if ((await bitmap(page)).hash === expectedHash) return;
		await pause(25);
	}
	assert.fail(`Bitmap did not reach hash ${expectedHash}`);
}

async function canvasRect(page) {
	const canvas = await page.$('canvas[aria-label="Drawing surface"]');
	const rect = await canvas?.boundingBox();
	assert.ok(rect, 'drawing canvas must be visible');
	return rect;
}

async function tap(page, x = 0.5, y = 0.5) {
	const rect = await canvasRect(page);
	await page.mouse.click(rect.x + rect.width * x, rect.y + rect.height * y);
}

async function stroke(page, from, to, steps = 4) {
	const rect = await canvasRect(page);
	await page.mouse.move(rect.x + rect.width * from[0], rect.y + rect.height * from[1]);
	await page.mouse.down();
	await page.mouse.move(rect.x + rect.width * to[0], rect.y + rect.height * to[1], { steps });
	await page.mouse.up();
}

// A lasso drag through fractional canvas points, the way Select area records
// it: several segments, each well past the 3 pixel spacing the mask keeps.
async function lasso(page, points) {
	const rect = await canvasRect(page);
	await page.mouse.move(rect.x + rect.width * points[0][0], rect.y + rect.height * points[0][1]);
	await page.mouse.down();
	for (const point of points.slice(1)) {
		await page.mouse.move(rect.x + rect.width * point[0], rect.y + rect.height * point[1], {
			steps: 4
		});
	}
	await page.mouse.up();
}

// A synthetic pointer event dispatched straight on the canvas, for pointer
// types and pressures puppeteer's mouse/touchscreen cannot produce (pen,
// pressure, a second concurrent touch). Untrusted events still reach plain
// addEventListener handlers and setPointerCapture, so the component's own
// logic runs exactly as it would for a real pen or finger.
async function dispatchPointer(page, type, { pointerId, pointerType, pressure = 0, x, y }) {
	await page.evaluate(
		(args) => {
			const canvas = document.querySelector('canvas[aria-label="Drawing surface"]');
			const rect = canvas.getBoundingClientRect();
			canvas.dispatchEvent(
				new PointerEvent(args.type, {
					bubbles: true,
					cancelable: true,
					pointerId: args.pointerId,
					pointerType: args.pointerType,
					pressure: args.pressure,
					isPrimary: true,
					button: 0,
					buttons: 1,
					clientX: rect.left + rect.width * args.x,
					clientY: rect.top + rect.height * args.y
				})
			);
		},
		{ type, pointerId, pointerType, pressure, x, y }
	);
}

// A full pen or touch gesture through canvas-fraction points, each with its
// own pressure (ignored for a touch pointer).
async function pointerStroke(page, pointerType, pointerId, points) {
	await dispatchPointer(page, 'pointerdown', { pointerId, pointerType, ...points[0] });
	for (const point of points.slice(1)) {
		await dispatchPointer(page, 'pointermove', { pointerId, pointerType, ...point });
	}
	await dispatchPointer(page, 'pointerup', { pointerId, pointerType, ...points.at(-1) });
}

// Arm Select area and drag the polygon. Erase selection only mounts once the
// released drag was accepted as a mask, so its appearance is the wait.
async function selectRegion(page, points) {
	await clickButton(page, 'Select area');
	await lasso(page, points);
	await page.waitForFunction(
		() =>
			[...document.querySelectorAll('button')].some(
				(candidate) => candidate.textContent?.trim() === 'Erase selection'
			),
		{ timeout: WAIT_MS }
	);
}

async function connect(page) {
	await page.type('#realtime-prompt', 'Canvas history test');
	await page.evaluate(() =>
		[...document.querySelectorAll('button')]
			.find((candidate) => candidate.textContent?.trim() === 'Connect')
			?.click()
	);
	await page.waitForFunction(() => document.body.innerText.includes('Active'), {
		timeout: WAIT_MS
	});
}

// The picture the region-erase tests share: a red stroke inside the coming
// selection, a blue one outside it, and a lasso around the left half only.
async function drawAndSelect(page) {
	await connect(page);
	await setBrushSize(page, 12);
	await selectColor(page, 'Red');
	await stroke(page, [0.05, 0.5], [0.45, 0.5], 6);
	await selectColor(page, 'Blue');
	await stroke(page, [0.55, 0.5], [0.95, 0.5], 6);
	await selectRegion(page, [
		[0.02, 0.25],
		[0.5, 0.25],
		[0.5, 0.75],
		[0.02, 0.75]
	]);
}

async function expectOutput(page, rgb) {
	await page
		.waitForFunction(
			(expected) => {
				const canvas = document.querySelectorAll('canvas')[1];
				const pixel = canvas?.getContext('2d').getImageData(256, 256, 1, 1).data;
				return (
					pixel?.[3] === 255 &&
					expected.every((value, index) => Math.abs(pixel[index] - value) <= 12)
				);
			},
			{},
			rgb
		)
		.catch(async (error) => {
			const actual = await page.evaluate(() => [
				...document.querySelectorAll('canvas')[1].getContext('2d').getImageData(256, 256, 1, 1).data
			]);
			assert.fail(`Expected output near ${rgb}; got ${actual}. ${error.message}`);
		});
}

async function clickButton(page, name) {
	await page.waitForFunction(
		(expected) =>
			[...document.querySelectorAll('button')].some(
				(candidate) =>
					(candidate.textContent?.trim() === expected ||
						candidate.getAttribute('aria-label') === expected) &&
					!candidate.disabled
			),
		{ timeout: WAIT_MS },
		name
	);
	await page.evaluate((expected) => {
		const candidate = [...document.querySelectorAll('button')].find(
			(element) =>
				element.textContent?.trim() === expected || element.getAttribute('aria-label') === expected
		);
		if (!candidate) throw new Error(`button ${expected} not found`);
		candidate.click();
	}, name);
}

async function clickStudioView(page, view, sectionSelector = null) {
	const before = await page.evaluate(() => new URL(location.href).searchParams.get('view'));
	const button = view === 'metrics' ? 'Metrics' : 'Images';
	await clickButton(page, button);
	await page.waitForFunction(
		({ expectedView, leavingImages, sectionSelector: readySelector }) => {
			const currentView = new URL(location.href).searchParams.get('view');
			const imageSectionMounted = Boolean(document.querySelector('[aria-label="Filter by model"]'));
			const gridMounted = Boolean(document.querySelector('.grid-scroller'));
			const lineageMounted = Boolean(document.querySelector('.lineage-viewport'));
			return (
				currentView === expectedView &&
				(!leavingImages || (!imageSectionMounted && !gridMounted && !lineageMounted)) &&
				(!readySelector || Boolean(document.querySelector(readySelector)))
			);
		},
		{ timeout: WAIT_MS },
		{ expectedView: view, leavingImages: before === 'images', sectionSelector }
	);
}

async function visibleGridTile(page) {
	return page.evaluate(() => {
		const scroller = document.querySelector('.grid-scroller')?.getBoundingClientRect();
		const tile = [...document.querySelectorAll('.grid-tile')].find((candidate) => {
			const rect = candidate.getBoundingClientRect();
			return scroller && rect.top > scroller.top && rect.bottom < scroller.bottom;
		});
		if (!tile) return null;
		const rect = tile.getBoundingClientRect();
		return {
			id: tile.dataset.generationId,
			x: rect.left + rect.width / 2,
			y: rect.top + rect.height / 2
		};
	});
}

async function maybeClickButton(page, name) {
	const state = await button(page, name);
	if (state.disabled) return false;
	await page.evaluate((expected) => {
		const candidate = [...document.querySelectorAll('button')].find(
			(element) =>
				element.textContent?.trim() === expected || element.getAttribute('aria-label') === expected
		);
		candidate?.click();
	}, name);
	return true;
}

// The tool rail is a group of toggle buttons, one per drawing tool, keyed by
// data-tool; picking one presses it and releases the others.
async function selectTool(page, tool) {
	await page.waitForSelector(`button[data-tool="${tool}"]`, { timeout: WAIT_MS });
	// A DOM click, like the select it replaced: the mouse stays where a test
	// holding a gesture left it.
	await page.$eval(`button[data-tool="${tool}"]`, (button) => button.click());
	assert.deepEqual(
		await page.$$eval('button[data-tool]', (buttons) =>
			buttons
				.filter((button) => button.getAttribute('aria-pressed') === 'true')
				.map((button) => button.dataset.tool)
		),
		[tool]
	);
}

async function selectColor(page, name) {
	await page.waitForFunction(
		(expected) => {
			return [...document.querySelectorAll('button, [role="radio"]')].some(
				(element) => element.getAttribute('aria-label') === expected
			);
		},
		{ timeout: WAIT_MS },
		name
	);
	await page.evaluate((expected) => {
		const candidate = [...document.querySelectorAll('button, [role="radio"]')].find(
			(element) => element.getAttribute('aria-label') === expected
		);
		if (!candidate) throw new Error(`color ${expected} not found`);
		candidate.click();
	}, name);
}

async function setBrushSize(page, value) {
	await page.waitForFunction(
		() => {
			const candidates = [...document.querySelectorAll('input, [role="slider"]')];
			return candidates.some((element) => {
				if (element.getAttribute('aria-label') === 'Brush size') return true;
				const labelledBy = element.getAttribute('aria-labelledby');
				return labelledBy
					?.split(/\s+/)
					.some((id) => document.getElementById(id)?.textContent?.trim() === 'Brush size');
			});
		},
		{ timeout: WAIT_MS }
	);
	await page.evaluate(() => {
		const candidates = [...document.querySelectorAll('input, [role="slider"]')];
		const brush = candidates.find((element) => {
			if (element.getAttribute('aria-label') === 'Brush size') return true;
			const labelledBy = element.getAttribute('aria-labelledby');
			return labelledBy
				?.split(/\s+/)
				.some((id) => document.getElementById(id)?.textContent?.trim() === 'Brush size');
		});
		brush?.focus();
	});
	await page.keyboard.press('Home');
	const minimum = await page.evaluate(() => {
		const brush = [...document.querySelectorAll('input, [role="slider"]')].find(
			(element) =>
				element.getAttribute('aria-label') === 'Brush size' ||
				element
					.getAttribute('aria-labelledby')
					?.split(/\s+/)
					.some((id) => document.getElementById(id)?.textContent?.trim() === 'Brush size')
		);
		return Number(brush?.getAttribute('min') ?? 1);
	});
	for (let next = minimum; next < value; next += 1) await page.keyboard.press('ArrowRight');
	assert.deepEqual(await brushInfo(page), { value: String(value), min: '1', max: '32' });
}

async function brushInfo(page) {
	return page.evaluate(() => {
		const brush = [...document.querySelectorAll('input, [role="slider"]')].find(
			(element) =>
				element.getAttribute('aria-label') === 'Brush size' ||
				element
					.getAttribute('aria-labelledby')
					?.split(/\s+/)
					.some((id) => document.getElementById(id)?.textContent?.trim() === 'Brush size')
		);
		if (!brush) return null;
		return {
			value: brush.getAttribute('value') ?? brush.getAttribute('aria-valuenow'),
			min: brush.getAttribute('min') ?? brush.getAttribute('aria-valuemin'),
			max: brush.getAttribute('max') ?? brush.getAttribute('aria-valuemax')
		};
	});
}

async function button(page, name) {
	await page.waitForFunction(
		(expected) =>
			[...document.querySelectorAll('button')].some(
				(candidate) =>
					candidate.textContent?.trim() === expected ||
					candidate.getAttribute('aria-label') === expected
			),
		{ timeout: WAIT_MS },
		name
	);
	return page.evaluate((expected) => {
		const candidate = [...document.querySelectorAll('button')].find(
			(element) =>
				element.textContent?.trim() === expected || element.getAttribute('aria-label') === expected
		);
		if (!candidate) throw new Error(`button ${expected} not found`);
		return { disabled: candidate.disabled };
	}, name);
}

async function waitForDrawingDownload(directory) {
	const deadline = Date.now() + WAIT_MS;
	while (Date.now() < deadline) {
		const file = (await readdir(directory)).find((name) => name.endsWith('.json'));
		if (file) return join(directory, file);
		await pause(25);
	}
	assert.fail('Save drawing did not download a JSON file');
}

async function openDrawingFile(page, filePath) {
	const chooserPromise = page.waitForFileChooser();
	await clickButton(page, 'Open drawing');
	const chooser = await chooserPromise;
	await chooser.accept([filePath]);
}

async function expectInvalidDrawingFile(page, filePath) {
	await page.waitForFunction(
		() => !document.body.innerText.includes('This drawing file is invalid or too large.'),
		{ timeout: WAIT_MS }
	);
	await openDrawingFile(page, filePath);
	await page.waitForFunction(
		() => document.body.innerText.includes('This drawing file is invalid or too large.'),
		{ timeout: WAIT_MS }
	);
}

test('canvas tab readiness survives paused animation frames', async () => {
	const harness = await openCanvas(
		'en',
		() => {
			const requestFrame = window.requestAnimationFrame.bind(window);
			const queuedFrames = [];
			window.requestAnimationFrame = (callback) => {
				queuedFrames.push(callback);
				return queuedFrames.length;
			};
			document.addEventListener(
				'click',
				(event) => {
					const button = event.target.closest('button');
					if (button?.textContent?.trim() !== 'Realtime canvas') return;
					window.requestAnimationFrame = requestFrame;
					for (const callback of queuedFrames.splice(0)) callback(performance.now());
					window.__canvasTabReleasedFrames = true;
				},
				{ capture: true, once: true }
			);
		},
		{
			apiAnswers: { '/api/v1/account': { body: [], delayMs: 500 } },
			waitUntil: 'domcontentloaded'
		}
	);
	try {
		const { page } = harness;
		assert.equal(await page.evaluate(() => window.__canvasTabReleasedFrames), true);
		assert.ok(await page.$('canvas[aria-label="Drawing surface"]'));
		assert.deepEqual(await surfacePixel(page, 256, 256), [255, 255, 255, 255]);
	} finally {
		await harness.close();
	}
});

test('a rectangle drag has one undo step and no stale preview edges', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await selectTool(page, 'rectangle');
		await selectColor(page, 'Blue');
		await setBrushSize(page, 8);
		await stroke(page, [0.2, 0.2], [0.8, 0.8], 8);
		const drawn = await bitmap(page);
		assert.notEqual(drawn.hash, blank.hash);
		assert.deepEqual(
			drawn.center,
			[255, 255, 255, 255],
			'moving the preview must not leave inner edges'
		);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		assert.equal((await button(page, 'Undo')).disabled, true);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, drawn.hash);
	} finally {
		await harness.close();
	}
});

test('line rectangle and ellipse drags draw outline-only geometry in either direction', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const shapes = [
			{
				tool: 'line',
				from: [0.75, 0.75],
				to: [0.25, 0.25],
				edge: [256, 256]
			},
			{
				tool: 'rectangle',
				from: [0.75, 0.75],
				to: [0.25, 0.25],
				edge: [256, 128],
				interior: [
					[256, 256],
					[205, 154]
				]
			},
			{
				tool: 'ellipse',
				from: [0.75, 0.75],
				to: [0.25, 0.25],
				edge: [256, 128],
				interior: [
					[256, 256],
					[205, 154]
				]
			}
		];
		for (const shape of shapes) {
			const blank = await bitmap(page);
			await selectTool(page, shape.tool);
			await stroke(page, shape.from, shape.to, 8);
			assert.deepEqual(await surfacePixel(page, ...shape.edge), [17, 24, 39, 255]);
			for (const interior of shape.interior ?? [])
				assert.deepEqual(
					await surfacePixel(page, ...interior),
					[255, 255, 255, 255],
					`${shape.tool} must not fill its interior or leave a preview edge`
				);
			await clickButton(page, 'Undo');
			await waitForBitmap(page, blank.hash);
			assert.equal((await button(page, 'Undo')).disabled, true);
		}
	} finally {
		await harness.close();
	}
});

test('a shape keeps its starting color and width through a mid-gesture control change', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await selectTool(page, 'rectangle');
		await selectColor(page, 'Red');
		await setBrushSize(page, 4);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height * 0.4, { steps: 2 });
		await selectColor(page, 'Blue');
		await setBrushSize(page, 32);
		await page.mouse.move(rect.x + rect.width * 0.8, rect.y + rect.height * 0.8, { steps: 4 });
		await page.mouse.up();
		assert.deepEqual(await surfacePixel(page, 256, 102), [220, 38, 38, 255]);
		assert.deepEqual(await surfacePixel(page, 410, 256), [220, 38, 38, 255]);
		assert.deepEqual(await surfacePixel(page, 256, 256), [255, 255, 255, 255]);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		assert.equal((await button(page, 'Undo')).disabled, true);
	} finally {
		await harness.close();
	}
});

test('shape draw and erase operations interleave with exact undo and redo bitmaps', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await selectColor(page, 'Red');
		await tap(page, 0.25, 0.5);
		const drawn = await bitmap(page);
		await selectTool(page, 'rectangle');
		await selectColor(page, 'Blue');
		await stroke(page, [0.2, 0.2], [0.8, 0.8], 8);
		const shaped = await bitmap(page);
		await selectTool(page, 'erase');
		await setBrushSize(page, 32);
		await tap(page, 0.5, 0.2);
		const erased = await bitmap(page);
		assert.notEqual(shaped.hash, erased.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, shaped.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, shaped.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, erased.hash);
	} finally {
		await harness.close();
	}
});

for (const finish of ['cancel', 'lost capture']) {
	test(`a ${finish} during a shape commits only the visible preview`, async () => {
		const harness = await openCanvas();
		try {
			const { page } = harness;
			const blank = await bitmap(page);
			const rect = await canvasRect(page);
			await selectTool(page, 'ellipse');
			await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
			await page.mouse.down();
			await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height * 0.4, { steps: 3 });
			const painted = await bitmap(page);
			if (finish === 'cancel') {
				await page.evaluate(() => {
					document.querySelector('canvas[aria-label="Drawing surface"]').dispatchEvent(
						new PointerEvent('pointercancel', {
							bubbles: true,
							pointerId: 1,
							isPrimary: true
						})
					);
				});
			} else {
				await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) =>
					canvas.releasePointerCapture(1)
				);
			}
			await page.mouse.move(rect.x + rect.width * 0.8, rect.y + rect.height * 0.8, { steps: 3 });
			await page.mouse.up();
			assert.deepEqual(await bitmap(page), painted);
			await clickButton(page, 'Undo');
			await waitForBitmap(page, blank.hash);
			await clickButton(page, 'Redo');
			await waitForBitmap(page, painted.hash);
		} finally {
			await harness.close();
		}
	});
}

test('saving a shape uses v2 geometry and round-trips its exact bitmap and redo', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-shape-save-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});
		await selectTool(page, 'ellipse');
		await selectColor(page, 'Purple');
		await setBrushSize(page, 10);
		await stroke(page, [0.2, 0.2], [1.1, 0.8], 8);
		const drawn = await bitmap(page);
		await clickButton(page, 'Save drawing');
		const savedPath = await waitForDrawingDownload(directory);
		const saved = JSON.parse(await readFile(savedPath, 'utf8'));
		assert.equal(saved.version, 4);
		assert.deepEqual([saved.width, saved.height], [512, 512]);
		assert.equal(saved.operations.length, 1);
		assert.equal(saved.operations[0].kind, 'shape');
		assert.equal(saved.operations[0].shape, 'ellipse');
		assert.equal(saved.operations[0].points.length, 2);
		assert.ok(
			saved.operations[0].points.some(({ x, y }) => x > 512 || y > 512),
			'captured shape coordinates must retain points outside the canvas'
		);

		await clickButton(page, 'Clear canvas');
		await waitForBitmap(page, blank.hash);
		await openDrawingFile(page, savedPath);
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, drawn.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('a zero-length shape gesture saves, reopens and undoes safely', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-shape-degenerate-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});
		await selectTool(page, 'ellipse');
		await stroke(page, [0.5, 0.5], [0.5, 0.5], 1);
		const degenerate = await bitmap(page);
		await clickButton(page, 'Save drawing');
		const savedPath = await waitForDrawingDownload(directory);
		const saved = JSON.parse(await readFile(savedPath, 'utf8'));
		assert.equal(saved.version, 4);
		assert.equal(saved.operations.length, 1);
		assert.equal(saved.operations[0].points.length, 2);
		await clickButton(page, 'Clear canvas');
		await waitForBitmap(page, blank.hash);
		await openDrawingFile(page, savedPath);
		await waitForBitmap(page, degenerate.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, degenerate.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('mixed shape and brush history uses the native PNG checkpoint at sixteen operations', async () => {
	const harness = await openCanvas('en', () => {
		const toBlob = HTMLCanvasElement.prototype.toBlob;
		const drawImage = CanvasRenderingContext2D.prototype.drawImage;
		window.__shapeCheckpointRequests = 0;
		window.__shapeCheckpointRestores = 0;
		HTMLCanvasElement.prototype.toBlob = function (callback, type, quality) {
			if (type === 'image/png') window.__shapeCheckpointRequests += 1;
			return toBlob.call(this, callback, type, quality);
		};
		CanvasRenderingContext2D.prototype.drawImage = function (...args) {
			if (
				this.canvas.getAttribute('aria-label') === 'Drawing surface' &&
				args[0] instanceof ImageBitmap
			)
				window.__shapeCheckpointRestores += 1;
			return drawImage.apply(this, args);
		};
	});
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const snapshots = [blank];
		for (let group = 0; group < 4; group += 1) {
			const offset = group * 0.18;
			await selectTool(page, 'draw');
			await tap(page, 0.1 + offset, 0.1 + offset);
			snapshots.push(await bitmap(page));
			await selectTool(page, 'line');
			await stroke(page, [0.15 + offset, 0.2], [0.3 + offset, 0.2], 3);
			snapshots.push(await bitmap(page));
			await selectTool(page, 'rectangle');
			await stroke(page, [0.2 + offset, 0.3], [0.32 + offset, 0.42], 3);
			snapshots.push(await bitmap(page));
			await selectTool(page, 'ellipse');
			await stroke(page, [0.4 + offset, 0.35], [0.52 + offset, 0.47], 3);
			snapshots.push(await bitmap(page));
		}
		await page.waitForFunction(() => window.__shapeCheckpointRequests === 1);
		for (let index = snapshots.length - 2; index >= 0; index -= 1) {
			await clickButton(page, 'Undo');
			assert.deepEqual(await bitmap(page), snapshots[index]);
		}
		for (let index = 1; index < snapshots.length; index += 1) {
			await clickButton(page, 'Redo');
			assert.deepEqual(await bitmap(page), snapshots[index]);
		}
		assert.equal(await page.evaluate(() => window.__shapeCheckpointRequests), 1);
		assert.ok(await page.evaluate(() => window.__shapeCheckpointRestores > 0));
	} finally {
		await harness.close();
	}
});

test('malformed v2 shapes and v1 shape operations are rejected atomically', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-shape-invalid-'));
	const downloadDirectory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-shape-valid-'));
	try {
		const harness = await openCanvas();
		let valid;
		try {
			const { page } = harness;
			const client = await page.createCDPSession();
			await client.send('Browser.setDownloadBehavior', {
				behavior: 'allow',
				downloadPath: downloadDirectory
			});
			await selectTool(page, 'rectangle');
			await stroke(page, [0.2, 0.2], [0.8, 0.8], 6);
			await clickButton(page, 'Save drawing');
			const validPath = await waitForDrawingDownload(downloadDirectory);
			valid = JSON.parse(await readFile(validPath, 'utf8'));
		} finally {
			await harness.close();
		}
		const malformed = structuredClone(valid);
		malformed.operations[0].points = [malformed.operations[0].points[0]];
		const malformedPath = join(directory, 'malformed-shape.potocolom.json');
		await writeFile(malformedPath, JSON.stringify(malformed));
		const v1Shape = structuredClone(valid);
		v1Shape.version = 1;
		const v1ShapePath = join(directory, 'v1-shape.potocolom.json');
		await writeFile(v1ShapePath, JSON.stringify(v1Shape));

		for (const path of [malformedPath, v1ShapePath]) {
			const caseHarness = await openCanvas();
			try {
				const { page: casePage } = caseHarness;
				await tap(casePage, 0.25, 0.25);
				await tap(casePage, 0.7, 0.3);
				const redoTarget = await bitmap(casePage);
				await clickButton(casePage, 'Undo');
				const current = await bitmap(casePage);
				await expectInvalidDrawingFile(casePage, path);
				assert.deepEqual(
					await bitmap(casePage),
					current,
					'invalid shape changed the current pixels'
				);
				assert.equal((await button(casePage, 'Undo')).disabled, false);
				assert.equal((await button(casePage, 'Redo')).disabled, false);
				await clickButton(casePage, 'Redo');
				await waitForBitmap(casePage, redoTarget.hash);
			} finally {
				await caseHarness.close();
			}
		}
	} finally {
		await rm(directory, { recursive: true, force: true });
		await rm(downloadDirectory, { recursive: true, force: true });
	}
});

test('a valid v1 stroke imports with its expected color and remains undoable', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-v1-stroke-'));
	const path = join(directory, 'stroke-v1.potocolom.json');
	await writeFile(
		path,
		JSON.stringify({
			version: 1,
			width: 512,
			height: 512,
			operations: [
				{
					kind: 'stroke',
					id: 'operation-1',
					mode: 'draw',
					color: '#dc2626',
					size: 12,
					points: [{ x: 128, y: 128 }]
				}
			],
			cursor: 1
		})
	);
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await openDrawingFile(page, path);
		await page.waitForFunction(() => {
			const pixel = document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d')
				.getImageData(128, 128, 1, 1).data;
			return pixel[0] === 220 && pixel[1] === 38 && pixel[2] === 38 && pixel[3] === 255;
		});
		const imported = await bitmap(page);
		assert.deepEqual(await surfacePixel(page, 128, 128), [220, 38, 38, 255]);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, imported.hash);
		assert.deepEqual(await surfacePixel(page, 128, 128), [220, 38, 38, 255]);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('erase selection paints paper inside the lasso and leaves the stroke outside it', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await drawAndSelect(page);
		assert.deepEqual(await surfacePixel(page, 128, 256), [220, 38, 38, 255]);
		assert.deepEqual(await surfacePixel(page, 384, 256), [37, 99, 235, 255]);
		const drawn = await bitmap(page);
		await clickButton(page, 'Erase selection');
		const erased = await bitmap(page);
		assert.notEqual(erased.hash, drawn.hash);
		assert.deepEqual(
			await surfacePixel(page, 128, 256),
			[255, 255, 255, 255],
			'the selection interior must be paper'
		);
		assert.deepEqual(
			await surfacePixel(page, 384, 256),
			[37, 99, 235, 255],
			'the stroke outside the selection must survive'
		);
		assert.equal(
			await page.evaluate(() =>
				[...document.querySelectorAll('button')].some(
					(candidate) => candidate.textContent?.trim() === 'Erase selection'
				)
			),
			true,
			'the selection stays after erasing inside it'
		);
	} finally {
		await harness.close();
	}
});

test('undo brings back the stroke a region erase covered and redo erases it again', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await drawAndSelect(page);
		const drawn = await bitmap(page);
		await clickButton(page, 'Erase selection');
		const erased = await bitmap(page);
		assert.notEqual(erased.hash, drawn.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, drawn.hash);
		assert.deepEqual(await surfacePixel(page, 128, 256), [220, 38, 38, 255]);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, erased.hash);
		assert.deepEqual(await surfacePixel(page, 128, 256), [255, 255, 255, 255]);
	} finally {
		await harness.close();
	}
});

test('a saved file round-trips its erase-region and exact bitmap', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-erase-save-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});
		await drawAndSelect(page);
		const drawn = await bitmap(page);
		await clickButton(page, 'Erase selection');
		const erased = await bitmap(page);
		await clickButton(page, 'Save drawing');
		const savedPath = await waitForDrawingDownload(directory);
		const saved = JSON.parse(await readFile(savedPath, 'utf8'));
		assert.equal(saved.version, 4);
		assert.deepEqual([saved.width, saved.height], [512, 512]);
		assert.deepEqual(
			saved.operations.map((operation) => operation.kind),
			['stroke', 'stroke', 'erase-region']
		);
		const region = saved.operations[2];
		assert.deepEqual(Object.keys(region).sort(), ['id', 'kind', 'points']);
		assert.ok(region.points.length >= 3 && region.points.length <= 512);
		await clickButton(page, 'Clear canvas');
		await waitForBitmap(page, blank.hash);
		await openDrawingFile(page, savedPath);
		await waitForBitmap(page, erased.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, erased.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('version 1 and version 2 drawing files still open', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-older-versions-'));
	const redDot = {
		kind: 'stroke',
		id: 'operation-1',
		mode: 'draw',
		color: '#dc2626',
		size: 12,
		points: [{ x: 128, y: 128 }]
	};
	const cases = [
		{
			name: 'v1-stroke',
			file: { version: 1, width: 512, height: 512, operations: [redDot], cursor: 1 },
			pixels: [
				[
					[128, 128],
					[220, 38, 38, 255]
				]
			],
			steps: 1
		},
		{
			name: 'v2-shape',
			file: {
				version: 2,
				width: 512,
				height: 512,
				operations: [
					redDot,
					{
						kind: 'shape',
						id: 'operation-2',
						shape: 'line',
						color: '#111827',
						size: 8,
						points: [
							{ x: 256, y: 64 },
							{ x: 256, y: 448 }
						]
					}
				],
				cursor: 2
			},
			pixels: [
				[
					[128, 128],
					[220, 38, 38, 255]
				],
				[
					[256, 128],
					[17, 24, 39, 255]
				]
			],
			steps: 2
		}
	];
	try {
		for (const entry of cases) {
			const path = join(directory, `${entry.name}.potocolom.json`);
			await writeFile(path, JSON.stringify(entry.file));
			const caseHarness = await openCanvas();
			try {
				const { page } = caseHarness;
				const blank = await bitmap(page);
				await openDrawingFile(page, path);
				for (const [[x, y], expected] of entry.pixels) {
					await page.waitForFunction(
						([pixelX, pixelY, red, green, blue]) => {
							const pixel = document
								.querySelector('canvas[aria-label="Drawing surface"]')
								?.getContext('2d')
								.getImageData(pixelX, pixelY, 1, 1).data;
							return (
								pixel?.[0] === red && pixel[1] === green && pixel[2] === blue && pixel[3] === 255
							);
						},
						{ timeout: WAIT_MS },
						[x, y, ...expected]
					);
					assert.deepEqual(await surfacePixel(page, x, y), expected, `${entry.name} pixel`);
				}
				const opened = await bitmap(page);
				for (let index = 0; index < entry.steps; index += 1) await clickButton(page, 'Undo');
				await waitForBitmap(page, blank.hash);
				for (let index = 0; index < entry.steps; index += 1) await clickButton(page, 'Redo');
				await waitForBitmap(page, opened.hash);
			} finally {
				await caseHarness.close();
			}
		}
	} finally {
		await rm(directory, { recursive: true, force: true });
	}
});

test('erase-region files gate on version and on their own three keys', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-erase-invalid-'));
	const validStroke = {
		kind: 'stroke',
		id: 'operation-1',
		mode: 'draw',
		color: '#111827',
		size: 6,
		points: [{ x: 128, y: 128 }]
	};
	const validRegion = {
		kind: 'erase-region',
		id: 'operation-2',
		points: [
			{ x: 64, y: 64 },
			{ x: 448, y: 64 },
			{ x: 448, y: 448 },
			{ x: 64, y: 448 }
		]
	};
	const invalid = [
		['version-2-with-erase-region', { version: 2 }],
		[
			'erase-region-with-two-points',
			{ operations: [validStroke, { ...validRegion, points: validRegion.points.slice(0, 2) }] }
		],
		[
			'erase-region-with-513-points',
			{
				operations: [
					validStroke,
					{
						...validRegion,
						points: Array.from({ length: 513 }, (_, index) => ({ x: index, y: index }))
					}
				]
			}
		],
		['erase-region-with-extra-key', { operations: [validStroke, { ...validRegion, mode: 'draw' }] }]
	];
	try {
		for (const [name, overrides] of invalid) {
			const path = join(directory, `${name}.potocolom.json`);
			await writeFile(
				path,
				JSON.stringify({
					version: 3,
					width: 512,
					height: 512,
					operations: [validStroke, validRegion],
					cursor: 2,
					...overrides
				})
			);
			const harness = await openCanvas();
			try {
				const { page } = harness;
				await tap(page, 0.25, 0.25);
				const first = await bitmap(page);
				await tap(page, 0.75, 0.75);
				const second = await bitmap(page);
				await clickButton(page, 'Undo');
				await waitForBitmap(page, first.hash);
				await expectInvalidDrawingFile(page, path);
				assert.deepEqual(await bitmap(page), first, `${name} replaced the current bitmap`);
				assert.equal((await button(page, 'Undo')).disabled, false, `${name} changed undo state`);
				assert.equal((await button(page, 'Redo')).disabled, false, `${name} changed redo state`);
				await clickButton(page, 'Redo');
				await waitForBitmap(page, second.hash);
			} finally {
				await harness.close();
			}
		}
	} finally {
		await rm(directory, { recursive: true, force: true });
	}
});

test('shape selection is silent and a connected shape publishes an opaque 512px WebP', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await connect(page);
		await pause(700);
		const before = await page.evaluate(() => window.__historySockets[0].frames.length);
		await selectTool(page, 'rectangle');
		await pause(700);
		assert.equal(await page.evaluate(() => window.__historySockets[0].frames.length), before);
		await stroke(page, [0.2, 0.2], [0.8, 0.8], 8);
		await page.waitForFunction(
			(minimum) => window.__historySockets[0].frames.length > minimum,
			{ timeout: WAIT_MS },
			before
		);
		const decoded = await page.evaluate(async (header) => {
			const frame = window.__historySockets[0].frames.at(-1);
			const image = await createImageBitmap(
				new Blob([frame.slice(header)], { type: 'image/webp' })
			);
			const surface = document.createElement('canvas');
			surface.width = image.width;
			surface.height = image.height;
			const context = surface.getContext('2d');
			context.drawImage(image, 0, 0);
			const alphaOpaque = [];
			const pixels = context.getImageData(0, 0, image.width, image.height).data;
			for (let index = 3; index < pixels.length; index += 4)
				alphaOpaque.push(pixels[index] === 255);
			const edge = [...context.getImageData(256, 102, 1, 1).data];
			const result = {
				kind: frame[0],
				width: image.width,
				height: image.height,
				alphaOpaque: alphaOpaque.every(Boolean),
				edge
			};
			image.close();
			return result;
		}, FRAME_HEADER_BYTES);
		assert.deepEqual(decoded.kind, 1);
		assert.deepEqual([decoded.width, decoded.height], [512, 512]);
		assert.equal(decoded.alphaOpaque, true);
		assert.ok(
			[17, 24, 39].every((channel, index) => Math.abs(decoded.edge[index] - channel) <= 12),
			'the published frame must contain the rectangle edge in its default color'
		);
		assert.equal(decoded.edge[3], 255);
	} finally {
		await harness.close();
	}
});

test('leaving the canvas while a shape preview is active drops the preview before navigation', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await connect(page);
		await pause(700);
		await selectTool(page, 'rectangle');
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.6, rect.y + rect.height * 0.6, { steps: 4 });
		assert.notDeepEqual(await bitmap(page), blank);
		await clickButton(page, 'Generate');
		await clickButton(page, 'Realtime canvas');
		await page.waitForSelector('canvas[aria-label="Drawing surface"]');
		await page.mouse.up();
		await waitForBitmap(page, blank.hash);
	} finally {
		await harness.close();
	}
});

test('saved drawing reopens with exact pixels and undo redo history', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});

		await selectColor(page, 'Red');
		await tap(page, 0.25, 0.25);
		const first = await bitmap(page);
		await selectColor(page, 'Blue');
		await tap(page, 0.75, 0.75);
		const second = await bitmap(page);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, first.hash);

		await clickButton(page, 'Save drawing');
		const savedPath = await waitForDrawingDownload(directory);
		assert.equal(basename(savedPath), 'drawing.potocolom.json');
		assert.match(await readFile(savedPath, 'utf8'), /"version"\s*:\s*4/);

		let unloadWarning = '';
		page.once('dialog', (dialog) => {
			unloadWarning = dialog.type();
			void dialog.accept();
		});
		await page.reload({ waitUntil: 'networkidle0' });
		assert.equal(unloadWarning, 'beforeunload', 'leaving a drawn canvas must warn first');
		await page.waitForFunction(
			(name) =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === name
				),
			{ timeout: WAIT_MS },
			'Realtime canvas'
		);
		await page.evaluate(
			(name) =>
				[...document.querySelectorAll('button')]
					.find((button) => button.textContent?.trim() === name)
					?.click(),
			'Realtime canvas'
		);
		await page.waitForSelector('canvas[aria-label="Drawing surface"]', { timeout: WAIT_MS });
		await waitForBitmap(page, blank.hash);
		await openDrawingFile(page, savedPath);
		await waitForBitmap(page, first.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, second.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('saved captured strokes, erase and a clear redo retain exact pixels', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-geometry-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});
		await setBrushSize(page, 20);
		await selectColor(page, 'Red');
		await stroke(page, [0.2, 0.5], [1.1, 0.5]);
		const drawn = await bitmap(page);
		await selectTool(page, 'erase');
		await stroke(page, [0.5, 0.3], [0.5, 0.7]);
		const erased = await bitmap(page);
		assert.notEqual(erased.hash, drawn.hash);
		await clickButton(page, 'Clear canvas');
		await waitForBitmap(page, blank.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, erased.hash);
		await clickButton(page, 'Save drawing');
		const saved = await waitForDrawingDownload(directory);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, blank.hash);
		await openDrawingFile(page, saved);
		await waitForBitmap(page, erased.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, erased.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, blank.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('save commits an active stroke and cancelling open leaves it active', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-gesture-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height * 0.4, { steps: 3 });
		const beforeCancel = await bitmap(page);
		const chooserPromise = page.waitForFileChooser();
		await clickButton(page, 'Open drawing');
		await (await chooserPromise).cancel();
		assert.deepEqual(await bitmap(page), beforeCancel);
		await page.mouse.move(rect.x + rect.width * 0.7, rect.y + rect.height * 0.7, { steps: 3 });
		const drawn = await bitmap(page);
		assert.notEqual(drawn.hash, beforeCancel.hash);
		await clickButton(page, 'Save drawing');
		const saved = await waitForDrawingDownload(directory);
		await page.mouse.move(rect.x + rect.width * 0.8, rect.y + rect.height * 0.2, { steps: 3 });
		await page.mouse.up();
		assert.deepEqual(await bitmap(page), drawn, 'save finishes the gesture');
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		await openDrawingFile(page, saved);
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('a viewport shrink during a captured stroke still produces a file that opens', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-resize-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: directory
		});
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.setViewport({ width: 1440, height: 500 });
		const smallRect = await canvasRect(page);
		assert.ok(
			smallRect.width > 0 && smallRect.height > 0,
			'canvas borders retain a nonzero pointer coordinate divisor'
		);
		await page.mouse.move(500, 250, { steps: 3 });
		await page.mouse.up();
		const drawn = await bitmap(page);
		await page.setViewport({ width: 1440, height: 1100 });
		await clickButton(page, 'Save drawing');
		const saved = await waitForDrawingDownload(directory);
		await clickButton(page, 'Clear canvas');
		await openDrawingFile(page, saved);
		await waitForBitmap(page, drawn.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('a failed save reports a save error and preserves the drawing', async () => {
	const harness = await openCanvas('en', () => {
		URL.createObjectURL = () => {
			throw new Error('download unavailable');
		};
	});
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await tap(page);
		const drawn = await bitmap(page);
		await clickButton(page, 'Save drawing');
		await page.waitForFunction(() =>
			document.body.innerText.includes(
				'The drawing could not be saved. Your drawing is still open.'
			)
		);
		assert.deepEqual(await bitmap(page), drawn);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
	} finally {
		await harness.close();
	}
});

test('a malformed drawing file preserves an active stroke and its history', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-invalid-'));
	const invalidPath = join(directory, 'invalid.potocolom.json');
	await writeFile(invalidPath, '{not valid json', 'utf8');
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.3, rect.y + rect.height * 0.3, { steps: 3 });
		const beforeInvalid = await bitmap(page);

		await openDrawingFile(page, invalidPath);
		await page.waitForFunction(
			() => document.body.innerText.includes('This drawing file is invalid or too large.'),
			{ timeout: WAIT_MS }
		);
		assert.deepEqual(await bitmap(page), beforeInvalid);

		await page.mouse.move(rect.x + rect.width * 0.7, rect.y + rect.height * 0.7, { steps: 3 });
		await page.mouse.up();
		assert.notEqual((await bitmap(page)).hash, beforeInvalid.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('invalid drawing schemas preserve the current bitmap and redo state', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-schema-'));
	const validStroke = {
		kind: 'stroke',
		id: 'operation-1',
		mode: 'draw',
		color: '#111827',
		size: 6,
		points: [{ x: 128, y: 128 }]
	};
	const invalidFiles = [
		['wrong-version', { version: 99 }],
		['wrong-dimensions', { width: 511 }],
		['cursor-outside-journal', { cursor: 2 }],
		[
			'duplicate-ids',
			{ operations: [validStroke, { ...validStroke, points: [{ x: 256, y: 256 }] }], cursor: 2 }
		],
		['nonfinite-point', { operations: [{ ...validStroke, points: [{ x: 1e400, y: 1 }] }] }],
		['invalid-hex-color', { operations: [{ ...validStroke, color: '#12345g' }] }],
		['invalid-size', { operations: [{ ...validStroke, size: 33 }] }],
		[
			'too-many-operations',
			{
				operations: Array.from({ length: 10001 }, (_, index) => ({
					kind: 'clear',
					id: `operation-${index + 1}`
				}))
			}
		],
		[
			'too-many-points',
			{
				operations: [
					{ ...validStroke, points: Array.from({ length: 200001 }, () => ({ x: 1, y: 1 })) }
				]
			}
		],
		['too-many-bytes', { padding: 'x'.repeat(8 * 1024 * 1024) }]
	];
	const paths = [];
	for (const [name, overrides] of invalidFiles) {
		const path = join(directory, `${name}.potocolom.json`);
		if (name === 'nonfinite-point') {
			const file = {
				version: 1,
				width: 512,
				height: 512,
				operations: [{ ...validStroke, points: [{ x: 1, y: 1 }] }],
				cursor: 1
			};
			await writeFile(path, JSON.stringify(file).replace('"x":1', '"x":1e400'), 'utf8');
		} else {
			await writeFile(
				path,
				JSON.stringify({
					version: 1,
					width: 512,
					height: 512,
					operations: [validStroke],
					cursor: 1,
					...overrides
				}),
				'utf8'
			);
		}
		paths.push([name, path]);
	}
	try {
		for (const [name, path] of paths) {
			const harness = await openCanvas();
			try {
				const { page } = harness;
				await tap(page, 0.25, 0.25);
				const first = await bitmap(page);
				await tap(page, 0.75, 0.75);
				const second = await bitmap(page);
				await clickButton(page, 'Undo');
				await waitForBitmap(page, first.hash);
				await expectInvalidDrawingFile(page, path);
				assert.deepEqual(await bitmap(page), first, `${name} replaced the current bitmap`);
				assert.equal((await button(page, 'Undo')).disabled, false, `${name} changed undo state`);
				assert.equal((await button(page, 'Redo')).disabled, false, `${name} changed redo state`);
				await clickButton(page, 'Redo');
				await waitForBitmap(page, second.hash);
			} finally {
				await harness.close();
			}
		}
	} finally {
		await rm(directory, { recursive: true, force: true });
	}
});

test('the file chooser adds no invisible keyboard stop', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await page.evaluate(() =>
			[...document.querySelectorAll('button')]
				.find((candidate) => candidate.textContent?.trim() === 'Save drawing')
				.focus()
		);
		await page.keyboard.press('Tab');
		assert.equal(
			await page.evaluate(() => document.activeElement.textContent.trim()),
			'Open drawing'
		);
		await page.keyboard.press('Tab');
		assert.equal(
			await page.evaluate(() => document.activeElement?.matches('input[type="file"]')),
			false
		);
	} finally {
		await harness.close();
	}
});

test('an imported large operation id does not prevent new strokes being saved', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-id-'));
	const sourcePath = join(directory, 'source.json');
	const source = {
		version: 1,
		width: 512,
		height: 512,
		cursor: 1,
		operations: [{ kind: 'clear', id: 'operation-9007199254740990' }]
	};
	await writeFile(sourcePath, JSON.stringify(source));
	const downloadDirectory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-id-saved-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: downloadDirectory
		});
		await openDrawingFile(page, sourcePath);
		await page.waitForFunction(() =>
			[...document.querySelectorAll('button')].some(
				(candidate) => candidate.textContent?.trim() === 'Undo' && !candidate.disabled
			)
		);
		for (let index = 0; index < 4; index += 1) await tap(page, 0.2 + index * 0.15, 0.5);
		const drawn = await bitmap(page);
		await clickButton(page, 'Save drawing');
		const savedPath = await waitForDrawingDownload(downloadDirectory);
		const saved = JSON.parse(await readFile(savedPath, 'utf8'));
		assert.equal(saved.operations[0].id, source.operations[0].id);
		assert.equal(new Set(saved.operations.map((operation) => operation.id)).size, 5);
		await clickButton(page, 'Clear canvas');
		await openDrawingFile(page, savedPath);
		await waitForBitmap(page, drawn.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
		await rm(downloadDirectory, { recursive: true, force: true });
	}
});

test('saving is silent and opening a blank drawing sends a complete white WebP', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-wire-'));
	const downloads = await mkdtemp(join(tmpdir(), 'potocolom-canvas-save-'));
	const blankPath = join(directory, 'blank.potocolom.json');
	await writeFile(
		blankPath,
		JSON.stringify({ version: 1, width: 512, height: 512, operations: [], cursor: 0 })
	);
	const harness = await openCanvas('en', () => {
		const toBlob = HTMLCanvasElement.prototype.toBlob;
		window.__webpEncodes = 0;
		HTMLCanvasElement.prototype.toBlob = function (callback, type, quality) {
			if (type === 'image/webp') window.__webpEncodes += 1;
			return toBlob.call(this, callback, type, quality);
		};
	});
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await tap(page);
		await openDrawingFile(page, blankPath);
		await waitForBitmap(page, blank.hash);
		assert.equal(await page.evaluate(() => window.__historySockets.length), 0);
		await connect(page);
		await selectColor(page, 'Blue');
		await setBrushSize(page, 32);
		await tap(page);
		await expectOutput(page, [37, 99, 235]);
		await pause(700);
		const counts = () =>
			page.evaluate(() => ({
				frames: window.__historySockets[0].frames.length,
				encodes: window.__webpEncodes,
				messages: window.__historySockets[0].sent.length
			}));
		const before = await counts();
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: downloads
		});
		await clickButton(page, 'Save drawing');
		await waitForDrawingDownload(downloads);
		await pause(700);
		assert.deepEqual(await counts(), before, 'save must not encode or send a frame');
		await openDrawingFile(page, blankPath);
		await waitForBitmap(page, blank.hash);
		await page.waitForFunction(
			(minimum) => window.__historySockets[0].frames.length > minimum,
			{},
			before.frames
		);
		await expectOutput(page, [255, 255, 255]);
		const decoded = await page.evaluate(async (header) => {
			const frame = window.__historySockets[0].frames.at(-1);
			const image = await createImageBitmap(
				new Blob([frame.slice(header)], { type: 'image/webp' })
			);
			const surface = document.createElement('canvas');
			surface.width = image.width;
			surface.height = image.height;
			const context = surface.getContext('2d');
			context.drawImage(image, 0, 0);
			const result = {
				kind: frame[0],
				width: image.width,
				height: image.height,
				white: context
					.getImageData(0, 0, image.width, image.height)
					.data.every((value) => value === 255)
			};
			image.close();
			return result;
		}, FRAME_HEADER_BYTES);
		assert.deepEqual(decoded, { kind: 1, width: 512, height: 512, white: true });
		await pause(700);
		assert.equal((await counts()).frames, before.frames + 1);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
		await rm(downloads, { recursive: true, force: true });
	}
});

for (const stage of ['encode', 'decode']) {
	test(`opening a drawing rejects a same-ID checkpoint pending ${stage}`, async () => {
		const sourceDirectory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-source-'));
		const targetDirectory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-target-'));
		const source = await openCanvas();
		let target;
		try {
			const client = await source.page.createCDPSession();
			await client.send('Browser.setDownloadBehavior', {
				behavior: 'allow',
				downloadPath: sourceDirectory
			});
			await selectColor(source.page, 'Blue');
			for (let index = 0; index < 16; index += 1)
				await tap(source.page, 0.1 + (index % 8) * 0.1, 0.3 + Math.floor(index / 8) * 0.1);
			const blue = await bitmap(source.page);
			await clickButton(source.page, 'Save drawing');
			const blueFile = JSON.parse(
				await readFile(await waitForDrawingDownload(sourceDirectory), 'utf8')
			);
			target = await openCanvas('en', () => {
				const toBlob = HTMLCanvasElement.prototype.toBlob;
				const decode = window.createImageBitmap;
				window.__checkpointRequests = 0;
				window.__checkpointTasks = [];
				window.__heldCheckpoints = [];
				window.__checkpointImages = [];
				HTMLCanvasElement.prototype.toBlob = function (callback, type, quality) {
					if (type !== 'image/png') return toBlob.call(this, callback, type, quality);
					window.__checkpointRequests += 1;
					return toBlob.call(
						this,
						(blob) => {
							const run = () => {
								const task = callback(blob);
								window.__checkpointTasks.push(task);
								return task;
							};
							if (window.__holdStage === 'encode') window.__heldCheckpoints.push(run);
							else run();
						},
						type
					);
				};
				window.createImageBitmap = async (...args) => {
					const image = await decode(...args);
					if (!(args[0] instanceof Blob) || args[0].type !== 'image/png') return image;
					window.__checkpointImages.push(image);
					if (window.__holdStage === 'decode')
						await new Promise((resolve) => window.__heldCheckpoints.push(resolve));
					return image;
				};
			});
			const { page } = target;
			await page.evaluate((value) => {
				window.__holdStage = value;
			}, stage);
			const downloadClient = await page.createCDPSession();
			await downloadClient.send('Browser.setDownloadBehavior', {
				behavior: 'allow',
				downloadPath: targetDirectory
			});
			await selectColor(page, 'Red');
			for (let index = 0; index < 16; index += 1)
				await tap(page, 0.1 + (index % 8) * 0.1, 0.3 + Math.floor(index / 8) * 0.1);
			await page.waitForFunction(() => window.__heldCheckpoints.length === 1);
			assert.notEqual((await bitmap(page)).hash, blue.hash);
			await clickButton(page, 'Save drawing');
			const redFile = JSON.parse(
				await readFile(await waitForDrawingDownload(targetDirectory), 'utf8')
			);
			blueFile.operations.forEach((operation, index) => {
				operation.id = redFile.operations[index].id;
			});
			const bluePath = join(sourceDirectory, 'same-ids.potocolom.json');
			await writeFile(bluePath, JSON.stringify(blueFile));
			await openDrawingFile(page, bluePath);
			await waitForBitmap(page, blue.hash);
			await selectColor(page, 'Green');
			for (let index = 0; index < 16; index += 1)
				await tap(page, 0.1 + (index % 8) * 0.1, 0.6 + Math.floor(index / 8) * 0.1);
			const edited = await bitmap(page);
			assert.equal(
				await page.evaluate(() => window.__checkpointRequests),
				1,
				'restore must retain the one-pending-job bound'
			);
			await page.evaluate(async () => {
				window.__holdStage = null;
				window.__heldCheckpoints.splice(0).forEach((release) => release());
				await Promise.all(window.__checkpointTasks);
			});
			assert.deepEqual(await bitmap(page), edited);
			assert.ok(
				await page.evaluate(() => window.__checkpointImages.every((image) => image.width === 0)),
				'stale decoded images must be closed'
			);
			if (stage === 'encode')
				assert.equal(await page.evaluate(() => window.__checkpointImages.length), 0);
			for (let index = 0; index < 16; index += 1) await clickButton(page, 'Undo');
			assert.deepEqual(
				await bitmap(page),
				blue,
				'stale red pixels cannot replace the opened blue drawing'
			);
			for (let index = 0; index < 16; index += 1) await clickButton(page, 'Redo');
			assert.deepEqual(await bitmap(page), edited);
		} finally {
			if (target) await target.close();
			await source.close();
			await rm(sourceDirectory, { recursive: true, force: true });
			await rm(targetDirectory, { recursive: true, force: true });
		}
	});
}

test('file read failure unlocks the drawing and a late read cannot change a new panel', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-read-'));
	const path = join(directory, 'blank.potocolom.json');
	await writeFile(
		path,
		JSON.stringify({ version: 1, width: 512, height: 512, operations: [], cursor: 0 })
	);
	const harness = await openCanvas('en', () => {
		const read = File.prototype.text;
		window.__fileReadMode = 'fail';
		window.__fileReads = [];
		File.prototype.text = async function () {
			if (window.__fileReadMode === 'fail') throw new Error('file read failed');
			const text = await read.call(this);
			return new Promise((resolve) => window.__fileReads.push(() => resolve(text)));
		};
	});
	try {
		const { page } = harness;
		await tap(page);
		const before = await bitmap(page);
		await expectInvalidDrawingFile(page, path);
		assert.deepEqual(await bitmap(page), before);
		await tap(page, 0.3, 0.3);
		const edited = await bitmap(page);
		assert.notEqual(edited.hash, before.hash);
		await page.evaluate(() => {
			window.__fileReadMode = 'hold';
		});
		await openDrawingFile(page, path);
		await page.waitForFunction(() => window.__fileReads.length === 1);
		assert.equal((await button(page, 'Save drawing')).disabled, true);
		assert.equal((await button(page, 'Undo')).disabled, true);
		await tap(page, 0.7, 0.7);
		assert.deepEqual(await bitmap(page), edited, 'pending read must lock drawing edits');
		await clickButton(page, 'Generate');
		await clickButton(page, 'Realtime canvas');
		await page.waitForSelector('canvas[aria-label="Drawing surface"]');
		await selectColor(page, 'Red');
		await tap(page);
		const fresh = await bitmap(page);
		await page.evaluate(async () => {
			window.__fileReads.splice(0).forEach((release) => release());
			await new Promise(requestAnimationFrame);
			await new Promise(requestAnimationFrame);
		});
		assert.deepEqual(await bitmap(page), fresh);
		assert.equal((await button(page, 'Save drawing')).disabled, false);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});

test('undo removes a complete tap and restores the exact white bitmap', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const before = await bitmap(page);
		await tap(page);
		await page.waitForFunction(
			() => {
				const canvas = document.querySelector('canvas[aria-label="Drawing surface"]');
				const pixel = canvas?.getContext('2d')?.getImageData(256, 256, 1, 1).data;
				return pixel?.[0] < 100 && pixel[1] < 130 && pixel[2] < 170 && pixel[3] === 255;
			},
			{ timeout: 3000 }
		);
		assert.deepEqual(
			await page.evaluate(() => [
				...document
					.querySelector('canvas[aria-label="Drawing surface"]')
					.getContext('2d')
					.getImageData(256, 256, 1, 1).data
			]),
			[17, 24, 39, 255]
		);
		assert.deepEqual((await button(page, 'Undo')).disabled, false);
		assert.equal((await button(page, 'Redo')).disabled, true);
		await page.evaluate(() => {
			[...document.querySelectorAll('button')]
				.find(
					(candidate) =>
						candidate.textContent?.trim() === 'Undo' ||
						candidate.getAttribute('aria-label') === 'Undo'
				)
				?.click();
		});
		await page.waitForFunction(() => {
			const canvas = document.querySelector('canvas[aria-label="Drawing surface"]');
			if (!canvas) return false;
			const data = canvas.getContext('2d').getImageData(0, 0, 512, 512).data;
			return data.every((value) => value === 255);
		});
		assert.deepEqual(await bitmap(page), before);
	} finally {
		await harness.close();
	}
});

test('redo restores exact pixels and a new stroke replaces the redo branch', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await tap(page, 0.25, 0.25);
		const first = await bitmap(page);
		await tap(page, 0.75, 0.75);
		const second = await bitmap(page);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, first.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, second.hash);
		await clickButton(page, 'Undo');
		await tap(page, 0.5, 0.5);
		await page.waitForFunction(() =>
			[...document.querySelectorAll('button')].some(
				(candidate) =>
					(candidate.textContent?.trim() === 'Redo' ||
						candidate.getAttribute('aria-label') === 'Redo') &&
					candidate.disabled
			)
		);
	} finally {
		await harness.close();
	}
});

test('clear, undo and redo follow the visible bitmap and blank clear is a no-op', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		assert.equal(await maybeClickButton(page, 'Clear canvas'), false);
		assert.deepEqual(await bitmap(page), blank);
		await tap(page, 0.5, 0.5);
		const drawn = await bitmap(page);
		await clickButton(page, 'Clear canvas');
		await waitForBitmap(page, blank.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, drawn.hash);
		await clickButton(page, 'Redo');
		await waitForBitmap(page, blank.hash);
		const redoBefore = await page.evaluate(() => {
			const button = [...document.querySelectorAll('button')].find(
				(candidate) =>
					candidate.textContent?.trim() === 'Redo' ||
					candidate.getAttribute('aria-label') === 'Redo'
			);
			return button?.disabled;
		});
		assert.equal(redoBefore, true);
		assert.equal(await maybeClickButton(page, 'Clear canvas'), false);
		assert.equal(
			await page.evaluate(() => {
				const button = [...document.querySelectorAll('button')].find(
					(candidate) =>
						candidate.textContent?.trim() === 'Redo' ||
						candidate.getAttribute('aria-label') === 'Redo'
				);
				return button?.disabled;
			}),
			true
		);
		await clickButton(page, 'Undo');
		assert.deepEqual(await bitmap(page), drawn, 'blank clear adds no history step');
	} finally {
		await harness.close();
	}
});

test('colors, brush bounds and erase replay keep each stroke settings', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const brushAttributes = await brushInfo(page);
		assert.ok(brushAttributes, 'Brush size must be keyboard accessible');
		assert.deepEqual(brushAttributes, { value: '6', min: '1', max: '32' });
		await page.click('#realtime-brush-size-label');
		const labelFocus = await page.evaluate(() => {
			const thumb = document.getElementById('realtime-brush-size');
			const labelledBy = thumb?.getAttribute('aria-labelledby');
			return {
				activeId: document.activeElement?.id,
				role: thumb?.getAttribute('role'),
				accessibleName: labelledBy
					? document.getElementById(labelledBy)?.textContent?.trim()
					: undefined
			};
		});
		assert.deepEqual(labelFocus, {
			activeId: 'realtime-brush-size',
			role: 'slider',
			accessibleName: 'Brush size'
		});
		await page.keyboard.press('ArrowRight');
		assert.equal((await brushInfo(page))?.value, '7');
		for (const name of ['Black', 'Red', 'Orange', 'Yellow', 'Green', 'Blue', 'Purple', 'Pink']) {
			await selectColor(page, name);
			const selected = await page.$eval(
				`[aria-label="${name}"]`,
				(element) =>
					element.getAttribute('aria-pressed') === 'true' ||
					element.getAttribute('aria-checked') === 'true'
			);
			assert.equal(selected, true, `${name} must expose selected state`);
		}
		await selectColor(page, 'Red');
		await setBrushSize(page, 12);
		await tap(page, 0.25, 0.25);
		const red = await page.evaluate(() => [
			...document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d')
				.getImageData(128, 128, 1, 1).data
		]);
		assert.deepEqual(red, [220, 38, 38, 255]);
		await selectTool(page, 'erase');
		await tap(page, 0.25, 0.25);
		const erased = await page.evaluate(() => [
			...document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d')
				.getImageData(128, 128, 1, 1).data
		]);
		assert.deepEqual(erased, [255, 255, 255, 255]);
		await clickButton(page, 'Undo');
		await page.waitForFunction(
			(expected) => {
				const pixel = document
					.querySelector('canvas[aria-label="Drawing surface"]')
					?.getContext('2d')
					.getImageData(128, 128, 1, 1).data;
				return (
					pixel && pixel[0] === expected[0] && pixel[1] === expected[1] && pixel[2] === expected[2]
				);
			},
			{ timeout: WAIT_MS },
			[220, 38, 38]
		);
		await selectTool(page, 'draw');
		await selectColor(page, 'Black');
		await setBrushSize(page, 6);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.5, rect.y + rect.height * 0.5);
		await page.mouse.down();
		await selectColor(page, 'Red');
		await setBrushSize(page, 32);
		await selectTool(page, 'erase');
		await page.mouse.move(rect.x + rect.width * 0.6, rect.y + rect.height * 0.5, { steps: 4 });
		await page.mouse.up();
		const retained = await page.evaluate(() => {
			const context = document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d');
			return {
				start: [...context.getImageData(256, 256, 1, 1).data],
				end: [...context.getImageData(280, 256, 1, 1).data],
				wide: [...context.getImageData(256, 246, 1, 1).data]
			};
		});
		assert.deepEqual(retained.start, [17, 24, 39, 255]);
		assert.deepEqual(retained.end, [17, 24, 39, 255]);
		assert.deepEqual(retained.wide, [255, 255, 255, 255]);
	} finally {
		await harness.close();
	}
});

test('display scaling, secondary pointers, hover and pointer cancellation stay bounded', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) => {
			canvas.style.width = '256px';
			canvas.style.height = '256px';
		});
		const blank = await bitmap(page);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
		assert.deepEqual(await bitmap(page), blank);
		await page.mouse.click(rect.x + rect.width / 2, rect.y + rect.height / 2, { button: 'right' });
		assert.deepEqual(await bitmap(page), blank);
		await page.mouse.move(rect.x + rect.width * 0.5, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.6, rect.y + rect.height * 0.2, { steps: 2 });
		await page.evaluate(() => {
			const canvas = document.querySelector('canvas[aria-label="Drawing surface"]');
			canvas.dispatchEvent(
				new PointerEvent('pointercancel', { bubbles: true, pointerId: 1, isPrimary: true })
			);
		});
		const canceled = await bitmap(page);
		await page.mouse.move(rect.x + rect.width * 0.9, rect.y + rect.height * 0.2, { steps: 2 });
		await page.mouse.up();
		assert.deepEqual(await bitmap(page), canceled);
		assert.notEqual(canceled.hash, blank.hash);
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
		const scaled = await canvasRect(page);
		await page.mouse.move(scaled.x + scaled.width * 0.5, scaled.y + scaled.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(scaled.x + scaled.width * 1.5, scaled.y - scaled.height, { steps: 3 });
		await page.mouse.up();
		const clippedEdge = await page.evaluate(() => {
			const data = document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d')
				.getImageData(500, 0, 1, 1).data;
			return [...data];
		});
		assert.deepEqual(clippedEdge, [255, 255, 255, 255]);
	} finally {
		await harness.close();
	}
});

test('paint controls are silent while drawing edits publish real WebP frames', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await connect(page);
		await pause(700);
		const before = await page.evaluate(() => ({
			frames: window.__historySockets[0]?.frames.length ?? 0,
			sent: window.__historySockets[0]?.sent.length ?? 0
		}));
		await selectColor(page, 'Blue');
		await setBrushSize(page, 20);
		await selectTool(page, 'erase');
		await pause(700);
		const afterControls = await page.evaluate(() => ({
			frames: window.__historySockets[0]?.frames.length ?? 0,
			sent: window.__historySockets[0]?.sent.length ?? 0
		}));
		assert.deepEqual(afterControls, before);
		await selectTool(page, 'draw');
		await tap(page, 0.5, 0.5);
		await page.waitForFunction(
			(minimum) => (window.__historySockets[0]?.frames.length ?? 0) > minimum,
			{
				timeout: WAIT_MS
			},
			before.frames
		);
		const frame = await page.evaluate(() => [...window.__historySockets[0].frames.at(-1)]);
		assert.equal(frame[0], 1, 'canvas frames retain the wire kind byte');
		assert.deepEqual(
			frame.slice(FRAME_HEADER_BYTES, FRAME_HEADER_BYTES + 4),
			[82, 73, 70, 70],
			'payload is a WebP RIFF image'
		);
		await expectOutput(page, [37, 99, 235]);
		const afterDraw = await page.evaluate(() => window.__historySockets[0].frames.length);
		await clickButton(page, 'Undo');
		await page.waitForFunction(
			(minimum) => (window.__historySockets[0]?.frames.length ?? 0) > minimum,
			{
				timeout: WAIT_MS
			},
			afterDraw
		);
		const afterUndo = await page.evaluate(() => window.__historySockets[0].frames.length);
		await expectOutput(page, [255, 255, 255]);
		await clickButton(page, 'Redo');
		await page.waitForFunction(
			(minimum) => (window.__historySockets[0]?.frames.length ?? 0) > minimum,
			{
				timeout: WAIT_MS
			},
			afterUndo
		);
		const beforeClear = await page.evaluate(() => window.__historySockets[0].frames.length);
		await expectOutput(page, [37, 99, 235]);
		await clickButton(page, 'Clear canvas');
		await page.waitForFunction(
			(minimum) => (window.__historySockets[0]?.frames.length ?? 0) > minimum,
			{
				timeout: WAIT_MS
			},
			beforeClear
		);
		const afterClear = await page.evaluate(() => window.__historySockets[0].frames.length);
		await expectOutput(page, [255, 255, 255]);
		assert.equal(await maybeClickButton(page, 'Clear canvas'), false);
		await pause(700);
		assert.equal(await page.evaluate(() => window.__historySockets[0].frames.length), afterClear);
	} finally {
		await harness.close();
	}
});

test('history and live edits survive disconnect and reconnect', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await connect(page);
		await tap(page, 0.2, 0.2);
		await tap(page, 0.8, 0.8);
		const drawn = await bitmap(page);
		await clickButton(page, 'Undo');
		const first = await bitmap(page);
		await clickButton(page, 'Redo');
		assert.deepEqual(await bitmap(page), drawn);
		await clickButton(page, 'Disconnect');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button')].some(
					(candidate) => candidate.textContent?.trim() === 'Connect' && !candidate.disabled
				),
			{ timeout: WAIT_MS }
		);
		assert.deepEqual(await bitmap(page), drawn);
		await connect(page);
		assert.deepEqual(await bitmap(page), drawn);
		await clickButton(page, 'Undo');
		assert.deepEqual(await bitmap(page), first);
		await clickButton(page, 'Redo');
		assert.deepEqual(await bitmap(page), drawn);
		await clickButton(page, 'Generate');
		await page.waitForFunction(() =>
			window.__historySockets.every((socket) => socket.readyState === 3)
		);
	} finally {
		await harness.close();
	}
});

test(
	'eighty independent strokes remain individually undoable through replay',
	{ timeout: 120000 },
	async () => {
		const harness = await openCanvas();
		try {
			const { page } = harness;
			const blank = await bitmap(page);
			let first;
			for (let index = 0; index < 80; index += 1) {
				const x = 0.08 + (index % 10) * 0.094;
				const y = 0.08 + Math.floor(index / 10) * 0.11;
				await tap(page, x, y);
				if (index === 0) first = await bitmap(page);
			}
			const all = await bitmap(page);
			for (let index = 0; index < 79; index += 1) await clickButton(page, 'Undo');
			assert.deepEqual(await bitmap(page), first);
			await clickButton(page, 'Undo');
			assert.deepEqual(await bitmap(page), blank);
			await clickButton(page, 'Redo');
			assert.deepEqual(await bitmap(page), first);
			for (let index = 1; index < 80; index += 1) await clickButton(page, 'Redo');
			assert.deepEqual(await bitmap(page), all);
		} finally {
			await harness.close();
		}
	}
);

test('new canvas controls have Spanish accessible labels', async () => {
	const harness = await openCanvas('es');
	try {
		const { page } = harness;
		assert.equal(await page.$eval('html', (element) => element.lang), 'es');
		for (const label of [
			'Deshacer',
			'Rehacer',
			'Guardar dibujo',
			'Abrir dibujo',
			'Tamaño del pincel',
			'Negro',
			'Rojo',
			'Naranja',
			'Amarillo',
			'Verde',
			'Azul',
			'Morado',
			'Rosa'
		]) {
			assert.ok(await page.$(`aria/${label}`), `missing Spanish label ${label}`);
		}
		assert.deepEqual(
			await page.$$eval('button[data-tool]', (buttons) =>
				buttons.map((button) => [button.dataset.tool, button.getAttribute('aria-label')])
			),
			[
				['draw', 'Dibujar'],
				['erase', 'Borrar'],
				['line', 'Línea'],
				['rectangle', 'Rectángulo'],
				['ellipse', 'Elipse']
			]
		);
	} finally {
		await harness.close();
	}
});

test('undo during a stroke ends that stroke and ignores later samples', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.35, rect.y + rect.height * 0.35, { steps: 2 });
		await clickButton(page, 'Undo');
		await page.mouse.move(rect.x + rect.width * 0.8, rect.y + rect.height * 0.8, { steps: 3 });
		await page.mouse.up();
		assert.deepEqual(await bitmap(page), blank);
		await tap(page, 0.8, 0.8);
		await clickButton(page, 'Undo');
		assert.deepEqual(await bitmap(page), blank);
	} finally {
		await harness.close();
	}
});

test('a secondary pointer cannot replace the active primary stroke', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.evaluate(() => {
			const canvas = document.querySelector('canvas[aria-label="Drawing surface"]');
			canvas.dispatchEvent(
				new PointerEvent('pointerdown', {
					bubbles: true,
					pointerId: 22,
					isPrimary: false,
					clientX: canvas.getBoundingClientRect().right - 20,
					clientY: canvas.getBoundingClientRect().bottom - 20
				})
			);
			canvas.dispatchEvent(
				new PointerEvent('pointermove', {
					bubbles: true,
					pointerId: 22,
					isPrimary: false,
					clientX: canvas.getBoundingClientRect().right - 5,
					clientY: canvas.getBoundingClientRect().bottom - 5
				})
			);
			canvas.dispatchEvent(
				new PointerEvent('pointerup', { bubbles: true, pointerId: 22, isPrimary: false })
			);
		});
		await page.mouse.move(rect.x + rect.width * 0.3, rect.y + rect.height * 0.3, { steps: 2 });
		await page.mouse.up();
		const pixelsAtSecondary = await page.evaluate(() => {
			const data = document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d')
				.getImageData(506, 506, 1, 1).data;
			return [...data];
		});
		assert.deepEqual(pixelsAtSecondary, [255, 255, 255, 255]);
		const continuedPrimary = await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) => [
			...canvas.getContext('2d').getImageData(150, 150, 1, 1).data
		]);
		assert.deepEqual(continuedPrimary, [17, 24, 39, 255]);
	} finally {
		await harness.close();
	}
});

test('clear stays disabled while erasing white paper and after erasing the last mark', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await selectTool(page, 'erase');
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
		await page.mouse.down();
		assert.deepEqual(await bitmap(page), blank);
		assert.equal((await button(page, 'Clear canvas')).disabled, true);
		await page.mouse.up();
		await selectTool(page, 'draw');
		await tap(page);
		await selectTool(page, 'erase');
		await setBrushSize(page, 32);
		await page.mouse.down();
		assert.deepEqual(await bitmap(page), blank);
		assert.equal((await button(page, 'Clear canvas')).disabled, true);
		await page.mouse.up();
	} finally {
		await harness.close();
	}
});

test('slow checkpoint encoding is bounded and a stale branch cannot change pixels', async () => {
	const harness = await openCanvas('en', () => {
		const toBlob = HTMLCanvasElement.prototype.toBlob;
		window.__heldCheckpoints = [];
		window.__checkpointRequests = 0;
		HTMLCanvasElement.prototype.toBlob = function (callback, type, quality) {
			if (type !== 'image/png') return toBlob.call(this, callback, type, quality);
			window.__checkpointRequests += 1;
			return toBlob.call(this, (blob) => window.__heldCheckpoints.push(() => callback(blob)), type);
		};
	});
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		for (let index = 0; index < 80; index += 1) {
			await tap(page, 0.1 + (index % 8) * 0.1, 0.1 + Math.floor(index / 8) * 0.08);
		}
		assert.equal(await page.evaluate(() => window.__checkpointRequests), 1);
		for (let index = 0; index < 80; index += 1) await clickButton(page, 'Undo');
		assert.deepEqual(await bitmap(page), blank);
		await selectColor(page, 'Red');
		await tap(page);
		const branch = await bitmap(page);
		await page.waitForFunction(() => window.__heldCheckpoints.length > 0);
		await page.evaluate(() => window.__heldCheckpoints.splice(0).forEach((release) => release()));
		await pause(100);
		assert.deepEqual(await bitmap(page), branch);
		await clickButton(page, 'Undo');
		assert.deepEqual(await bitmap(page), blank);
		await clickButton(page, 'Redo');
		assert.deepEqual(await bitmap(page), branch);
	} finally {
		await harness.close();
	}
});

test('ready checkpoints preserve drawn strokes and clear exactly, and are released on navigation', async () => {
	const harness = await openCanvas('en', () => {
		const decode = window.createImageBitmap;
		const drawImage = CanvasRenderingContext2D.prototype.drawImage;
		window.__checkpointImages = [];
		window.__checkpointRestores = 0;
		window.createImageBitmap = async (...args) => {
			const image = await decode(...args);
			if (args[0] instanceof Blob && args[0].type === 'image/png')
				window.__checkpointImages.push(image);
			return image;
		};
		CanvasRenderingContext2D.prototype.drawImage = function (...args) {
			if (this.canvas.getAttribute('aria-label') === 'Drawing surface')
				window.__checkpointRestores += 1;
			return drawImage.apply(this, args);
		};
	});
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const snapshots = [blank];
		for (let index = 0; index < 80; index += 1) {
			if (index % 16 === 15) await clickButton(page, 'Clear canvas');
			else {
				await selectColor(page, index % 2 ? 'Blue' : 'Red');
				await stroke(page, [0.2, 0.2 + (index % 10) * 0.05], [0.8, 0.8 - (index % 10) * 0.05], 5);
			}
			snapshots.push(await bitmap(page));
			if (index % 16 === 15) {
				await page.waitForFunction(
					(count) => window.__checkpointImages.length === count,
					{},
					(index + 1) / 16
				);
			}
		}
		for (let index = 79; index >= 0; index -= 1) {
			await clickButton(page, 'Undo');
			assert.deepEqual(await bitmap(page), snapshots[index], `undo to edit ${index}`);
		}
		for (let index = 1; index <= 80; index += 1) {
			await clickButton(page, 'Redo');
			assert.deepEqual(await bitmap(page), snapshots[index], `redo to edit ${index}`);
		}
		assert.ok(
			await page.evaluate(() => window.__checkpointRestores > 0),
			'ready checkpoints must be used'
		);
		assert.ok(
			await page.evaluate(
				() => window.__checkpointImages.filter((image) => image.width > 0).length <= 4
			)
		);
		await clickButton(page, 'Generate');
		await page.waitForFunction(() => window.__checkpointImages.every((image) => image.width === 0));
	} finally {
		await harness.close();
	}
});

test('a decode finishing after navigation releases its bitmap without touching a new drawing', async () => {
	const harness = await openCanvas('en', () => {
		const decode = window.createImageBitmap;
		window.__pendingCheckpointImages = [];
		window.__releaseCheckpointImages = [];
		window.createImageBitmap = async (...args) => {
			const image = await decode(...args);
			if (!(args[0] instanceof Blob) || args[0].type !== 'image/png') return image;
			window.__pendingCheckpointImages.push(image);
			await new Promise((resolve) => window.__releaseCheckpointImages.push(resolve));
			return image;
		};
	});
	try {
		const { page } = harness;
		for (let index = 0; index < 80; index += 1) await tap(page, 0.2 + (index % 6) * 0.1, 0.5);
		await page.waitForFunction(() => window.__pendingCheckpointImages.length > 0);
		assert.equal(await page.evaluate(() => window.__pendingCheckpointImages.length), 1);
		await clickButton(page, 'Generate');
		await clickButton(page, 'Realtime canvas');
		await page.waitForSelector('canvas[aria-label="Drawing surface"]');
		await selectColor(page, 'Red');
		await tap(page);
		const fresh = await bitmap(page);
		await page.evaluate(() => window.__releaseCheckpointImages.forEach((release) => release()));
		await page.waitForFunction(() =>
			window.__pendingCheckpointImages.every((image) => image.width === 0)
		);
		assert.deepEqual(await bitmap(page), fresh);
	} finally {
		await harness.close();
	}
});

test('losing pointer capture commits the visible stroke and ignores the remaining movement', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height * 0.4, { steps: 4 });
		const painted = await bitmap(page);
		await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) =>
			canvas.releasePointerCapture(1)
		);
		await page.mouse.move(rect.x + rect.width * 0.8, rect.y + rect.height * 0.8, { steps: 4 });
		await page.mouse.up();
		assert.deepEqual(await bitmap(page), painted);
		await clickButton(page, 'Undo');
		assert.deepEqual(await bitmap(page), blank);
		await clickButton(page, 'Redo');
		assert.deepEqual(await bitmap(page), painted);
	} finally {
		await harness.close();
	}
});

test('the whole drawing surface is visible on a narrow screen', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		for (const width of [390, 320]) {
			await page.setViewport({ width, height: 844 });
			await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) =>
				canvas.scrollIntoView({ block: 'center' })
			);
			const visible = await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) => {
				const rect = canvas.getBoundingClientRect();
				return [rect.top + 2, rect.bottom - 2].every(
					(y) => document.elementFromPoint(rect.x + rect.width / 2, y) === canvas
				);
			});
			assert.equal(visible, true, `canvas clipped at ${width}px`);
			assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
		}
	} finally {
		await harness.close();
	}
});

test('a round brush paints its stated width and controls preserve a pending redo', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await setBrushSize(page, 32);
		await tap(page);
		const shape = await page.$eval('canvas[aria-label="Drawing surface"]', (canvas) => {
			const context = canvas.getContext('2d');
			return [
				[264, 256],
				[274, 256],
				[271, 271]
			].map(([x, y]) => [...context.getImageData(x, y, 1, 1).data]);
		});
		assert.deepEqual(shape, [
			[17, 24, 39, 255],
			[255, 255, 255, 255],
			[255, 255, 255, 255]
		]);
		const painted = await bitmap(page);
		await clickButton(page, 'Undo');
		await selectColor(page, 'Red');
		await setBrushSize(page, 12);
		await selectTool(page, 'erase');
		assert.equal((await button(page, 'Redo')).disabled, false);
		await clickButton(page, 'Redo');
		assert.deepEqual(await bitmap(page), painted);
	} finally {
		await harness.close();
	}
});

for (const tool of ['erase', 'rectangle'])
	test(`${tool} movement does not read the full bitmap on every pointer sample`, async () => {
		const harness = await openCanvas('en', () => {
			const getImageData = CanvasRenderingContext2D.prototype.getImageData;
			window.__fullReadbacks = 0;
			CanvasRenderingContext2D.prototype.getImageData = function (...args) {
				if (
					this.canvas.getAttribute('aria-label') === 'Drawing surface' &&
					args[2] === 512 &&
					args[3] === 512
				)
					window.__fullReadbacks += 1;
				return getImageData.apply(this, args);
			};
		});
		try {
			const { page } = harness;
			if (tool === 'erase') {
				await tap(page, 0.8, 0.8);
				await selectTool(page, 'erase');
			} else await selectTool(page, 'rectangle');
			const rect = await canvasRect(page);
			await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
			await page.mouse.down();
			await page.evaluate(() => {
				window.__fullReadbacks = 0;
			});
			await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height * 0.4, { steps: 24 });
			assert.equal(await page.evaluate(() => window.__fullReadbacks), 0);
			await page.mouse.up();
			if (tool === 'erase') assert.ok(await page.evaluate(() => window.__fullReadbacks > 0));
		} finally {
			await harness.close();
		}
	});

test('finishing a checkpoint does not publish an extra live frame', async () => {
	const harness = await openCanvas('en', () => {
		const decode = window.createImageBitmap;
		window.createImageBitmap = async (...args) => {
			const image = await decode(...args);
			if (args[0] instanceof Blob && args[0].type === 'image/png') {
				await new Promise((resolve) => {
					window.__releaseLiveCheckpoint = resolve;
				});
				window.__liveCheckpointFinished = true;
			}
			return image;
		};
	});
	try {
		const { page } = harness;
		await connect(page);
		await setBrushSize(page, 20);
		for (let index = 0; index < 16; index += 1) await tap(page);
		await page.waitForFunction(() => !!window.__releaseLiveCheckpoint);
		await expectOutput(page, [17, 24, 39]);
		await pause(700);
		const before = await page.evaluate(() => ({
			frames: window.__historySockets[0].frames.length,
			controls: window.__historySockets[0].sent.length
		}));
		await page.evaluate(() => window.__releaseLiveCheckpoint());
		await page.waitForFunction(() => window.__liveCheckpointFinished);
		await pause(700);
		assert.deepEqual(
			await page.evaluate(() => ({
				frames: window.__historySockets[0].frames.length,
				controls: window.__historySockets[0].sent.length
			})),
			before
		);
	} finally {
		await harness.close();
	}
});

// Six succeeded history generations, each with its own prompt and an inline
// SVG data: URI thumbnail, plus two starred ones a test can load late.
const STRIP_COLORS = ['dc2626', 'ea580c', 'ca8a04', '16a34a', '2563eb', '9333ea'];

function stripThumbnail(label, color) {
	const svg =
		`<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">` +
		`<rect width="96" height="96" fill="#${color}"/>` +
		`<rect x="6" y="6" width="84" height="84" fill="none" stroke="#ffffff" stroke-width="4"/>` +
		`<text x="48" y="58" font-family="sans-serif" font-size="28" fill="#ffffff" ` +
		`text-anchor="middle">${label}</text></svg>`;
	return `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
}

function stripGeneration(id, prompt, label, color) {
	const thumbnail = stripThumbnail(label, color);
	return {
		id,
		model_id: MODEL.id,
		source_asset_id: null,
		params: { prompt },
		state: 'succeeded',
		progress: null,
		gpu_ms: 42,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: '2026-09-29T08:00:00Z',
		dispatched_at: '2026-09-29T08:00:00Z',
		finished_at: '2026-09-29T08:00:02Z',
		starred_at: null,
		expired_favorite: false,
		assets: [
			{
				id: `${id}-asset`,
				url: thumbnail,
				thumbnail_url: thumbnail,
				download_url: thumbnail,
				width: 96,
				height: 96
			}
		]
	};
}

const STRIP_HISTORY = Array.from({ length: 6 }, (_, index) =>
	stripGeneration(
		`strip-history-${index}`,
		`History strip prompt ${index + 1}`,
		String(index + 1),
		STRIP_COLORS[index]
	)
);

const STRIP_STARRED = Array.from({ length: 2 }, (_, index) =>
	stripGeneration(
		`strip-starred-${index}`,
		`Starred strip prompt ${index + 1}`,
		`S${index + 1}`,
		STRIP_COLORS[index]
	)
);

// The history list, the starred list (empty unless the test says otherwise)
// and the lineage answer a selection asks for, so the generate view can show
// the selected result without a render error.
function stripFixtures(starred = { body: [] }) {
	const emptyLineage = {
		ancestors: [],
		children: [],
		descendant_count: 0,
		descendants_truncated: false
	};
	return {
		'/api/v1/generations': { body: STRIP_HISTORY },
		'/api/v1/generations?starred=true': starred,
		...Object.fromEntries(
			STRIP_HISTORY.map((generation) => [
				`/api/v1/generations/${generation.id}/lineage`,
				{ body: emptyLineage }
			])
		)
	};
}

const LINEAGE_PIXEL = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="#dc2626"/></svg>')}`;
const LINEAGE_ROOT_JOB = '13000000-0000-4000-8000-000000000001';
const LINEAGE_FANOUT_JOB = '13000000-0000-4000-8000-000000000002';

function lineageJobId(index) {
	return `13000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
}

function lineageAssetId(index) {
	return `23000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
}

function lineageGeneration(
	id,
	assetId,
	prompt,
	createdAt,
	sourceAssetId = null,
	{ expired = false, hasDerivatives = false } = {}
) {
	return {
		id,
		model_id: MODEL.id,
		source_asset_id: sourceAssetId,
		has_derivatives: hasDerivatives,
		params: { prompt },
		state: 'succeeded',
		progress: null,
		gpu_ms: 42,
		input_fetch_ms: null,
		load_ms: null,
		postprocess_ms: null,
		failure_reason: null,
		created_at: createdAt,
		dispatched_at: createdAt,
		finished_at: createdAt,
		starred_at: null,
		expired,
		expired_favorite: false,
		assets:
			assetId === null || expired
				? []
				: [
						{
							id: assetId,
							url: LINEAGE_PIXEL,
							thumbnail_url: LINEAGE_PIXEL,
							download_url: LINEAGE_PIXEL,
							width: 8,
							height: 8
						}
					]
	};
}

function lineageSubtreeNode(generation, assetId, parentJobId, missing = false) {
	return {
		parent_job_id: parentJobId,
		output_asset_ids: [assetId],
		entry: {
			job_id: generation.id,
			asset_id: assetId,
			action: parentJobId === null ? 'generate' : 'image_to_image',
			model_id: MODEL.id,
			created_at: generation.created_at,
			state: generation.state,
			thumbnail_url: missing ? null : LINEAGE_PIXEL,
			missing
		},
		generation
	};
}

function lineageSubtree(nodes) {
	return {
		nodes,
		truncated: false,
		remaining_count_lower_bound: 0,
		max_depth: 100,
		max_nodes: 600
	};
}

function lineageCanvasFixtures() {
	const createdAt = (seconds) => new Date(Date.UTC(2026, 8, 29, 0, 0, seconds)).toISOString();
	const expiredAssetId = lineageAssetId(1);
	const expiredRoot = lineageGeneration(
		LINEAGE_ROOT_JOB,
		expiredAssetId,
		'Expired ancestor prompt',
		createdAt(100),
		null,
		{ expired: true, hasDerivatives: true }
	);
	const chainChild = lineageGeneration(
		lineageJobId(3),
		lineageAssetId(3),
		'First live descendant',
		createdAt(101),
		expiredAssetId
	);
	const chainGrandchild = lineageGeneration(
		lineageJobId(4),
		lineageAssetId(4),
		'Second live descendant',
		createdAt(102),
		lineageAssetId(3)
	);
	const fanoutAssetId = lineageAssetId(20);
	const fanoutRoot = lineageGeneration(
		LINEAGE_FANOUT_JOB,
		fanoutAssetId,
		'Fan-out prompt',
		createdAt(90),
		null,
		{ hasDerivatives: true }
	);
	const fanoutChildren = Array.from({ length: 5 }, (_, index) =>
		lineageGeneration(
			lineageJobId(21 + index),
			lineageAssetId(21 + index),
			`Fan-out child ${index + 1}`,
			createdAt(91 + index),
			fanoutAssetId
		)
	);
	const looseRoots = Array.from({ length: 6 }, (_, index) =>
		lineageGeneration(
			lineageJobId(101 + index),
			lineageAssetId(101 + index),
			`Loose root ${index + 1}`,
			createdAt(80 - index)
		)
	);
	const roots = [expiredRoot, fanoutRoot, ...looseRoots];
	const rootNode = lineageSubtreeNode(expiredRoot, expiredAssetId, null, true);
	const chainChildNode = lineageSubtreeNode(chainChild, lineageAssetId(3), LINEAGE_ROOT_JOB);
	const chainGrandchildNode = lineageSubtreeNode(
		chainGrandchild,
		lineageAssetId(4),
		lineageJobId(3)
	);
	const fanoutNodes = [lineageSubtreeNode(fanoutRoot, fanoutAssetId, null)];
	for (const child of fanoutChildren) {
		fanoutNodes.push(lineageSubtreeNode(child, child.assets[0].id, LINEAGE_FANOUT_JOB));
	}
	const answers = {
		'/api/v1/generations?roots_only=true&limit=50': { body: roots },
		[`/api/v1/generations/${LINEAGE_ROOT_JOB}/subtree`]: {
			body: lineageSubtree([rootNode, chainChildNode, chainGrandchildNode]),
			delayMs: 300
		},
		[`/api/v1/generations/${LINEAGE_FANOUT_JOB}/subtree`]: {
			body: lineageSubtree(fanoutNodes)
		}
	};
	return {
		answers,
		expiredAssetId,
		chainAssetIds: [lineageAssetId(3), lineageAssetId(4)],
		fanoutAssetIds: [fanoutAssetId, ...fanoutChildren.map((child) => child.assets[0].id)],
		looseAssetIds: looseRoots.map((root) => root.assets[0].id)
	};
}

function imageGridFixtures(count = 1) {
	const createdAt = new Date().toISOString();
	const rows = Array.from({ length: count }, (_, index) => {
		const generation = lineageGeneration(
			lineageJobId(201 + index),
			lineageAssetId(201 + index),
			`Image filter result ${index + 1}`,
			createdAt
		);
		generation.starred_at = createdAt;
		return generation;
	});
	let nextStarredAt = Date.now();
	const models = [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }];
	const answers = {
		'/api/v1/generations/models': { body: { models } },
		'/api/v1/generations?limit=50': { body: rows },
		'/api/v1/generations?limit=50&starred=true': { body: rows },
		'/api/v1/generations?starred=true&limit=200': {
			body: () => rows.filter((row) => row.starred_at !== null)
		},
		'/api/v1/generations?roots_only=true&limit=50': { body: [] },
		...Object.fromEntries(
			rows.map((row) => [
				`/api/v1/generations/${row.id}/star`,
				{
					body: (_url, request) => {
						if (request.method === 'DELETE') row.starred_at = null;
						else if (row.starred_at === null) {
							row.starred_at = new Date(++nextStarredAt).toISOString();
						}
						return {};
					}
				}
			])
		)
	};
	return { answers, rows, models };
}

async function openImagesGrid(apiAnswers, setup, setupArgs = []) {
	const harness = await launchStudio(
		'en',
		setup,
		apiAnswers,
		'domcontentloaded',
		'/app?view=images',
		setupArgs
	);
	try {
		await harness.page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		return harness;
	} catch (error) {
		await harness.close(false);
		throw error;
	}
}

async function openGenerateThenImages(apiAnswers, setup) {
	const harness = await launchStudio(
		'en',
		setup,
		apiAnswers,
		'domcontentloaded',
		'/app?view=generate'
	);
	try {
		const previousUrl = await harness.page.evaluate(() => location.href);
		await clickButton(harness.page, 'Images');
		await harness.page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		return { ...harness, previousUrl };
	} catch (error) {
		await harness.close(false);
		throw error;
	}
}

test('Grid keeps shared older history behind server pages and preserves order on More', async () => {
	const rows = Array.from({ length: 120 }, (_, index) =>
		lineageGeneration(
			lineageJobId(500 + index),
			lineageAssetId(500 + index),
			`Ordered library image ${String(index).padStart(3, '0')}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	let firstSharedHistoryRead = true;
	let failNextPage = false;
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': {
			body: (url, request) => {
				const cursor = url.searchParams.get('cursor');
				if (cursor !== null) {
					const index = rows.findIndex((row) => row.id === cursor);
					assert.notEqual(index, -1, 'shared-history and grid cursors belong to the fixture feed');
					return rows.slice(index + 1, index + 51);
				}
				if (firstSharedHistoryRead) {
					firstSharedHistoryRead = false;
					assert.equal(request.headers.referer?.includes('view=images'), true);
				}
				return rows.slice(0, 50);
			}
		},
		'/api/v1/generations?limit=50&cursor=': {
			body: (url) => {
				if (failNextPage) {
					failNextPage = false;
					throw new Error('simulated page failure');
				}
				const index = rows.findIndex((row) => row.id === url.searchParams.get('cursor'));
				assert.notEqual(index, -1, 'older-history cursors belong to the fixture feed');
				return rows.slice(index + 1, index + 51);
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': { body: [] },
		...Object.fromEntries(
			rows.map((row) => [
				`/api/v1/generations/${row.id}/lineage`,
				{ body: { ancestors: [], children: [] } }
			])
		)
	};
	const harness = await launchStudio(
		'en',
		() => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			window.__libraryPageRequests = [];
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (
					url.pathname === '/api/v1/generations' &&
					url.searchParams.get('limit') === '50' &&
					!url.searchParams.has('roots_only') &&
					!url.searchParams.has('fields')
				) {
					window.__libraryPageRequests.push(url.searchParams.toString());
				}
				return originalFetch(input, init);
			};
		},
		answers,
		'domcontentloaded',
		'/app?view=images'
	);
	try {
		const { page } = harness;
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await page.waitForFunction(() => window.__libraryPageRequests.length === 1, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'Generate');
		await clickButton(page, 'Older');
		await page.waitForFunction(
			() =>
				window.__libraryPageRequests.filter((query) => new URLSearchParams(query).has('cursor'))
					.length === 1,
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(
			() => document.querySelectorAll('button[data-strip-thumb]').length === 100,
			{ timeout: WAIT_MS }
		);
		await clickButton(page, 'Older');
		await page.waitForFunction(
			() =>
				window.__libraryPageRequests.filter((query) => new URLSearchParams(query).has('cursor'))
					.length === 2,
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(
			() =>
				![...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === 'Older'
				),
			{ timeout: WAIT_MS }
		);
		await clickButton(page, 'Images');
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await clickButton(page, 'Grid');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		let titles = await page.$$eval('.grid-tile', (tiles) => tiles.map((tile) => tile.title));
		assert.equal(titles[0], 'Ordered library image 000');
		assert.deepEqual(
			titles,
			rows.slice(0, 50).map((row) => row.params.prompt),
			'only the newest server page appears, in server order'
		);
		await clickButton(page, 'More matching results');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 100, {
			timeout: WAIT_MS
		});
		titles = await page.$$eval('.grid-tile', (tiles) => tiles.map((tile) => tile.title));
		assert.deepEqual(
			titles,
			rows.slice(0, 100).map((row) => row.params.prompt),
			'Load More appends the next server page without inserting older shared-history rows'
		);
		failNextPage = true;
		await clickButton(page, 'More matching results');
		await page.waitForSelector('[role="alert"]', { timeout: WAIT_MS });
		assert.equal(await page.$$eval('.grid-tile', (tiles) => tiles.length), 100);
		await clickButton(page, 'Retry');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 120, {
			timeout: WAIT_MS
		});
		titles = await page.$$eval('.grid-tile', (tiles) => tiles.map((tile) => tile.title));
		assert.deepEqual(
			titles,
			rows.map((row) => row.params.prompt)
		);
		assert.equal(await page.$('[role="alert"]'), null);
	} finally {
		await harness.close();
	}
});

async function chooseImageOption(page, aria, name) {
	await page.locator(`[aria-label="${aria}"]`).click();
	await page.waitForSelector('[role="option"]');
	const options = await page.$$('[role="option"]');
	let picked = false;
	for (const option of options) {
		if ((await option.evaluate((node) => node.textContent.trim())) === name) {
			await option.click();
			picked = true;
			break;
		}
	}
	assert.ok(picked, `Missing option ${name}`);
	const triggerExpanded = await page.$eval(`[aria-label="${aria}"]`, (trigger) =>
		trigger.getAttribute('aria-expanded')
	);
	if (triggerExpanded === 'true') await page.keyboard.press('Escape');
	await page.waitForFunction(() => !document.querySelector('[role="listbox"]'));
	await page.waitForFunction(() => getComputedStyle(document.body).pointerEvents !== 'none');
}

async function chooseImageModel(page, name) {
	await chooseImageOption(page, 'Filter by model', name);
}

test('grid, lineage roots, and search IDs exclude failed jobs before paging', async () => {
	const saved = lineageGeneration(
		lineageJobId(1100),
		lineageAssetId(1100),
		'Older saved image',
		new Date(Date.now() - 30 * 86400000).toISOString()
	);
	const failed = Array.from({ length: 55 }, (_, index) => {
		const generation = lineageGeneration(
			lineageJobId(1200 + index),
			lineageAssetId(1200 + index),
			`Recent failed job ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		);
		generation.state = 'failed';
		generation.assets = [];
		return generation;
	});
	const stateQueries = { grid: [], roots: [], search: [] };
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': {
			body: (url) => {
				stateQueries.grid.push(url.searchParams.toString());
				return url.searchParams.get('state') === 'succeeded' ? [saved] : failed.slice(0, 50);
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				stateQueries.roots.push(url.searchParams.toString());
				return url.searchParams.get('state') === 'succeeded' ? [saved] : failed.slice(0, 50);
			}
		},
		'/api/v1/generations?fields=ids&limit=5000': {
			body: (url) => {
				stateQueries.search.push(url.searchParams.toString());
				return {
					ids:
						url.searchParams.get('state') === 'succeeded'
							? [saved.id]
							: failed.slice(0, 50).map((generation) => generation.id)
				};
			}
		},
		[`/api/v1/generations/${saved.id}/subtree`]: {
			body: lineageSubtree([lineageSubtreeNode(saved, saved.assets[0].id, null)])
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__stateQueries = { grid: [], roots: [], search: [] };
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.get('state') === 'succeeded') {
				const query = url.searchParams.toString();
				if (url.searchParams.has('roots_only')) window.__stateQueries.roots.push(query);
				else if (url.searchParams.has('fields')) window.__stateQueries.search.push(query);
				else if (url.searchParams.get('limit') === '50') window.__stateQueries.grid.push(query);
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-generation-id="${saved.id}"]`, { timeout: WAIT_MS });
		assert.equal(await page.$$('.grid-tile').then((tiles) => tiles.length), 1);
		assert.equal(new URLSearchParams(stateQueries.grid.at(-1)).get('state'), 'succeeded');
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(
			() => document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.equal(stateQueries.roots.length, 0, 'Grid filter changes do not load lineage roots');
		await clickButton(page, 'Lineage');
		await page.waitForSelector(`[data-lineage-node="${saved.assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__stateQueries.roots.length === 1, {
			timeout: WAIT_MS
		});
		assert.equal(new URLSearchParams(stateQueries.roots.at(-1)).get('state'), 'succeeded');
		assert.equal(new URLSearchParams(stateQueries.roots.at(-1)).get('model_id'), MODEL.id);
		await page.type('input[name="q"]', 'Older saved image');
		await page.waitForFunction(() => window.__stateQueries.search.length === 1, {
			timeout: WAIT_MS
		});
		assert.equal(new URLSearchParams(stateQueries.search.at(-1)).get('state'), 'succeeded');
	} finally {
		await harness.close();
	}
});

test('lineage root requests abort on filter and mode changes, then use current criteria', async () => {
	let releaseRootPages;
	const rootPages = new Promise((resolvePages) => {
		releaseRootPages = resolvePages;
	});
	const root = lineageGeneration(
		lineageJobId(1300),
		lineageAssetId(1300),
		'Root request lifecycle',
		new Date().toISOString()
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': { body: [root] },
		'/api/v1/generations?roots_only=true&limit=50': {
			body: async () => {
				await rootPages;
				return [root];
			}
		},
		[`/api/v1/generations/${root.id}/subtree`]: {
			body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)])
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__rootLifecycle = { queries: [], aborts: 0 };
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
				window.__rootLifecycle.queries.push(url.searchParams.toString());
				init?.signal?.addEventListener('abort', () => {
					window.__rootLifecycle.aborts += 1;
				});
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector('.grid-tile', { timeout: WAIT_MS });
		await clickButton(page, 'Lineage');
		await page.waitForFunction(() => window.__rootLifecycle.queries.length === 1, {
			timeout: WAIT_MS
		});
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(() => window.__rootLifecycle.queries.length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__rootLifecycle.aborts >= 1, { timeout: WAIT_MS });
		await clickButton(page, 'Grid');
		await page.waitForFunction(() => window.__rootLifecycle.aborts >= 2, { timeout: WAIT_MS });
		await clickButton(page, 'Lineage');
		await page.waitForFunction(() => window.__rootLifecycle.queries.length === 3, {
			timeout: WAIT_MS
		});
		const latest = await page.evaluate(() => window.__rootLifecycle.queries.at(-1));
		assert.equal(new URLSearchParams(latest).get('state'), 'succeeded');
		assert.equal(new URLSearchParams(latest).get('model_id'), MODEL.id);
		releaseRootPages();
		await page.waitForSelector(`[data-lineage-node="${root.assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await pause(250);
		assert.equal(await page.evaluate(() => window.__rootLifecycle.queries.length), 3);
	} finally {
		releaseRootPages();
		await harness.close();
	}
});

test('a failed lineage replacement keeps its roots and retries the same query', async () => {
	const root = lineageGeneration(
		lineageJobId(1350),
		lineageAssetId(1350),
		'Retry current lineage roots',
		new Date().toISOString()
	);
	const rootQueries = [];
	let failReplacement = true;
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				if (url.searchParams.has('model_id') && failReplacement) {
					failReplacement = false;
					throw new Error('simulated root replacement failure');
				}
				return [root];
			}
		},
		[`/api/v1/generations/${root.id}/subtree`]: {
			body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)])
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
		window.__rootQueries = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
				window.__rootQueries.push(url.searchParams.toString());
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${root.assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.evaluate((id) => {
			window.__savedFailedRoot = document.querySelector(`[data-lineage-node="${id}"]`);
		}, root.assets[0].id);
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent.trim() === 'Retry'
				),
			{ timeout: WAIT_MS }
		);
		assert.equal(rootQueries.length, 2);
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('model_id'), MODEL.id);
		assert.equal(
			await page.evaluate(
				(id) => document.querySelector(`[data-lineage-node="${id}"]`) === window.__savedFailedRoot,
				root.assets[0].id
			),
			true,
			'a failed replacement keeps the existing root node mounted'
		);
		await clickButton(page, 'Retry');
		await page.waitForFunction(() => window.__rootQueries.length === 3, { timeout: WAIT_MS });
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('model_id'), MODEL.id);
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('state'), 'succeeded');
		await page.waitForFunction(
			() =>
				![...document.querySelectorAll('button')].some(
					(button) => button.textContent.trim() === 'Retry'
				),
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.evaluate(
				(id) => document.querySelector(`[data-lineage-node="${id}"]`) === window.__savedFailedRoot,
				root.assets[0].id
			),
			true,
			'retrying the same criteria preserves the keyed root'
		);
	} finally {
		await harness.close();
	}
});

test('a failed root append retries with its committed cursor', async () => {
	const roots = Array.from({ length: 60 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1620 + index),
			lineageAssetId(1620 + index),
			`Append root ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const rootQueries = [];
	let failAppend = true;
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				if (!url.searchParams.has('cursor')) return roots.slice(0, 50);
				if (failAppend) {
					failAppend = false;
					throw new Error('simulated root append failure');
				}
				const cursorIndex = roots.findIndex((root) => root.id === url.searchParams.get('cursor'));
				return roots.slice(cursorIndex + 1, cursorIndex + 51);
			}
		},
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
		window.__rootQueries = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
				window.__rootQueries.push(url.searchParams.toString());
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.focus('.lineage-viewport');
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowDown');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent.trim() === 'Retry'
				),
			{ timeout: WAIT_MS }
		);
		const failedQuery = new URLSearchParams(rootQueries.at(-1));
		assert.equal(failedQuery.get('cursor'), roots[49].id);
		await clickButton(page, 'Retry');
		await page.waitForFunction(() => window.__rootQueries.length === 3, { timeout: WAIT_MS });
		await page.waitForFunction(
			() =>
				![...document.querySelectorAll('button')].some(
					(button) => button.textContent.trim() === 'Retry'
				),
			{ timeout: WAIT_MS }
		);
		const retriedQuery = new URLSearchParams(rootQueries.at(-1));
		assert.equal(retriedQuery.get('cursor'), failedQuery.get('cursor'));
		assert.equal(retriedQuery.get('state'), 'succeeded');
	} finally {
		await harness.close();
	}
});

test('a failed root replacement cannot append its old cursor and retries from the first page', async () => {
	const roots = Array.from({ length: 300 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1450 + index),
			lineageAssetId(1450 + index),
			`Old root ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const replacement = lineageGeneration(
		lineageJobId(1600),
		lineageAssetId(1600),
		'New filtered root',
		new Date().toISOString()
	);
	const rootQueries = [];
	let failReplacement = true;
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				if (url.searchParams.has('model_id')) {
					if (failReplacement) {
						failReplacement = false;
						throw new Error('simulated root replacement failure');
					}
					return [replacement];
				}
				if (!url.searchParams.has('cursor')) return roots.slice(0, 50);
				const cursorIndex = roots.findIndex((root) => root.id === url.searchParams.get('cursor'));
				return roots.slice(cursorIndex + 1, cursorIndex + 51);
			}
		},
		...Object.fromEntries(
			[...roots, replacement].map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
		window.__rootQueries = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
				window.__rootQueries.push(url.searchParams.toString());
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.focus('.lineage-viewport');
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowDown');
		await page.waitForFunction(
			() => window.__rootQueries.some((query) => new URLSearchParams(query).has('cursor')),
			{ timeout: WAIT_MS }
		);
		const committedPageCount = await page.evaluate(() => window.__rootQueries.length);
		assert.ok(committedPageCount >= 2, 'more than fifty old roots are committed before failure');
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent.trim() === 'Retry'
				),
			{ timeout: WAIT_MS }
		);
		const countWithFailedReplace = await page.evaluate(() => window.__rootQueries.length);
		assert.equal(countWithFailedReplace, committedPageCount + 1);
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('cursor'), null);
		const viewport = await page.$('.lineage-viewport');
		const bounds = await viewport.boundingBox();
		assert.ok(bounds);
		await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
		await page.mouse.down();
		await page.mouse.move(bounds.x + bounds.width / 2 - 700, bounds.y + bounds.height / 2, {
			steps: 8
		});
		await page.mouse.up();
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowDown');
		await pause(250);
		assert.equal(
			await page.evaluate(() => window.__rootQueries.length),
			countWithFailedReplace,
			'panning while a replacement error is visible does not request an old page'
		);
		assert.equal(
			rootQueries.filter((query) => new URLSearchParams(query).has('cursor')).length,
			committedPageCount - 1,
			'only pages from the committed old query use cursors'
		);
		await clickButton(page, 'Retry');
		await page.waitForSelector(`[data-lineage-node="${replacement.assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		assert.equal(
			await page.evaluate(() => window.__rootQueries.length),
			countWithFailedReplace + 1
		);
		const retried = new URLSearchParams(rootQueries.at(-1));
		assert.equal(retried.get('model_id'), MODEL.id);
		assert.equal(retried.get('state'), 'succeeded');
		assert.equal(retried.get('cursor'), null);
	} finally {
		await harness.close();
	}
});

test('GPU history keeps only data cached for the selected range', async () => {
	let historyRequestCount = 0;
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/metrics/gpu/history?': {
			status: () => (historyRequestCount === 1 ? 200 : 503),
			body: (url) => {
				historyRequestCount += 1;
				if (historyRequestCount > 1) return { detail: 'history unavailable' };
				const from = Number(url.searchParams.get('from'));
				const to = Number(url.searchParams.get('to'));
				return {
					from: new Date(from).toISOString(),
					to: new Date(to).toISOString(),
					rollup: 'raw',
					samples: [
						{
							ts: new Date(to - 1000).toISOString(),
							util_pct: 47,
							vram_used_pct: 23,
							temperature_c: 48,
							power_w: 90
						}
					],
					truncated: false
				};
			}
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__gpuHistoryResponses = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname !== '/api/v1/metrics/gpu/history') return originalFetch(input, init);
			const entry = { query: url.search, status: null, completed: false };
			window.__gpuHistoryResponses.push(entry);
			return originalFetch(input, init).then((response) => {
				entry.status = response.status;
				entry.completed = true;
				return response;
			});
		};
	});
	try {
		const { page } = harness;
		await clickButton(page, 'Metrics');
		await page.waitForFunction(
			() =>
				document.body.innerText.includes('GPU activity') &&
				!document.body.innerText.includes('Sample data'),
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(
			() => window.__gpuHistoryResponses.length >= 1 && window.__gpuHistoryResponses[0].completed,
			{ timeout: WAIT_MS }
		);
		const firstRequest = await page.evaluate(() => window.__gpuHistoryResponses[0]);
		assert.equal(firstRequest.status, 200);
		const firstQuery = new URLSearchParams(firstRequest.query);
		assert.equal(firstQuery.get('rollup'), 'raw');
		assert.ok(
			Math.abs(Number(firstQuery.get('to')) - Number(firstQuery.get('from')) - 300_000) < 2000
		);

		await clickButton(page, 'Images');
		await page.waitForFunction(
			() =>
				new URL(location.href).searchParams.get('view') === 'images' &&
				Boolean(document.querySelector('[aria-label="Filter by model"]')),
			{ timeout: WAIT_MS }
		);
		const reentryIndex = await page.evaluate(() => window.__gpuHistoryResponses.length);
		await clickButton(page, 'Metrics');
		await page.waitForFunction(
			() => new URL(location.href).searchParams.get('view') === 'metrics',
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(
			(index) =>
				window.__gpuHistoryResponses.length > index &&
				window.__gpuHistoryResponses[index].completed,
			{ timeout: WAIT_MS },
			reentryIndex
		);
		const refreshResponse = await page.evaluate(
			(index) => window.__gpuHistoryResponses[index],
			reentryIndex
		);
		assert.equal(refreshResponse.status, 503);
		assert.equal(await page.evaluate(() => document.body.innerText.includes('Sample data')), false);
		const refreshQuery = new URLSearchParams(refreshResponse.query);
		assert.equal(refreshQuery.get('rollup'), 'raw');
		assert.ok(
			Math.abs(Number(refreshQuery.get('to')) - Number(refreshQuery.get('from')) - 300_000) < 2000
		);

		const uncachedIndex = await page.evaluate(() => window.__gpuHistoryResponses.length);
		await clickButton(page, '24h');
		await page.waitForFunction(
			(index) =>
				window.__gpuHistoryResponses.length > index &&
				window.__gpuHistoryResponses[index].completed,
			{ timeout: WAIT_MS },
			uncachedIndex
		);
		const uncachedResponse = await page.evaluate(
			(index) => window.__gpuHistoryResponses[index],
			uncachedIndex
		);
		assert.equal(uncachedResponse.status, 503);
		const uncachedQuery = new URLSearchParams(uncachedResponse.query);
		assert.equal(uncachedQuery.get('rollup'), 'auto');
		assert.ok(
			Math.abs(Number(uncachedQuery.get('to')) - Number(uncachedQuery.get('from')) - 86_400_000) <
				2000
		);
		await page.waitForFunction(() => document.body.innerText.includes('Sample data'), {
			timeout: WAIT_MS
		});
	} finally {
		await harness.close();
	}
});

test('choosing Images from the mobile command palette closes both overlays before navigation', async () => {
	const harness = await launchStudio('en', undefined, {}, 'domcontentloaded', '/app?view=generate');
	try {
		const { page } = harness;
		await page.setViewport({ width: 390, height: 844 });
		await clickButton(page, 'Toggle sidebar');
		await page.waitForFunction(() => document.querySelectorAll('[role="dialog"]').length === 1, {
			timeout: WAIT_MS
		});
		await page.click('button[aria-keyshortcuts="Meta+K Control+K"]');
		await page.waitForFunction(() => document.querySelectorAll('[role="dialog"]').length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForSelector('input[aria-label="Search or jump to"]', { timeout: WAIT_MS });
		await page.type('input[aria-label="Search or jump to"]', 'Images');
		await page.keyboard.press('Enter');
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await page.waitForFunction(() => document.querySelectorAll('[role="dialog"]').length === 0, {
			timeout: WAIT_MS
		});
		assert.equal(new URL(page.url()).searchParams.get('view'), 'images');
		await page.evaluate(() => history.back());
		await page.waitForFunction(
			() => new URL(location.href).searchParams.get('view') === 'generate',
			{ timeout: WAIT_MS }
		);
	} finally {
		await harness.close();
	}
});

test('returning to Lineage keeps committed pages and viewport until root filters change', async () => {
	const roots = Array.from({ length: 110 }, (_, index) => {
		const root = lineageGeneration(
			lineageJobId(1700 + index),
			lineageAssetId(1700 + index),
			`Cached root ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		);
		if (index < 12) root.model_id = 'other-root-model';
		return root;
	});
	const rootQueries = [];
	const answers = {
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-root-model', name: 'Other root model' }]
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				if (url.searchParams.has('model_id')) {
					return roots.filter((root) => root.model_id === 'other-root-model');
				}
				if (!url.searchParams.has('cursor')) return roots.slice(0, 50);
				const cursorIndex = roots.findIndex((root) => root.id === url.searchParams.get('cursor'));
				return roots.slice(cursorIndex + 1, cursorIndex + 51);
			}
		},
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await launchStudio(
		'en',
		() => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			window.__rootQueries = [];
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
					window.__rootQueries.push(url.searchParams.toString());
				}
				return originalFetch(input, init);
			};
		},
		answers,
		'domcontentloaded',
		'/app?view=generate'
	);
	try {
		const { page } = harness;
		await clickButton(page, 'Images');
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.focus('.lineage-viewport');
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowDown');
		await page.waitForFunction(
			() => window.__rootQueries.some((query) => new URLSearchParams(query).has('cursor')),
			{ timeout: WAIT_MS }
		);
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowUp');
		await page.waitForFunction(() => !document.activeViewTransition, { timeout: WAIT_MS });
		const panPoint = await page.$eval('.lineage-viewport', (element) => {
			const rect = element.getBoundingClientRect();
			for (let y = rect.top + 40; y < rect.bottom - 30; y += 16) {
				for (let x = rect.left + 60; x < rect.right - 30; x += 16) {
					const target = document.elementFromPoint(x, y);
					if (
						target &&
						element.contains(target) &&
						!target.closest('button, [data-lineage-node], [data-lineage-search]')
					) {
						return { x, y };
					}
				}
			}
			return null;
		});
		assert.ok(panPoint, 'found a background point away from nodes and buttons');
		const transformBeforePan = await page.$eval('.lineage-world', (world) => world.style.transform);
		await page.mouse.move(panPoint.x, panPoint.y);
		await page.mouse.down();
		await page.mouse.move(panPoint.x - 45, panPoint.y - 20, { steps: 6 });
		await page.mouse.up();
		let previousTransform = await page.$eval('.lineage-world', (world) => world.style.transform);
		let stableFrames = 0;
		const settleDeadline = Date.now() + WAIT_MS;
		while (stableFrames < 3) {
			assert.ok(Date.now() < settleDeadline, 'Lineage pan transform did not settle');
			await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
			const currentTransform = await page.$eval('.lineage-world', (world) => world.style.transform);
			stableFrames = currentTransform === previousTransform ? stableFrames + 1 : 0;
			previousTransform = currentTransform;
		}
		const before = await page.evaluate(() => {
			const node = document.querySelector('[data-lineage-node]');
			const rect = node.getBoundingClientRect();
			window.__modeNodeId = node.dataset.lineageNode;
			return {
				transform: document.querySelector('.lineage-world').style.transform,
				id: node.dataset.lineageNode,
				left: rect.left,
				top: rect.top
			};
		});
		assert.notEqual(
			before.transform,
			transformBeforePan,
			'a background drag changes the Lineage viewport before it is captured'
		);
		const queryCount = await page.evaluate(() => window.__rootQueries.length);
		await page.$eval(`[data-lineage-node="${before.id}"]`, (node) => node.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.equal(
			await page.evaluate(() => history.state?.['sveltekit:states']?.studioOverlay),
			'inspector'
		);
		await clickButton(page, 'Lineage');
		assert.notEqual(await page.$('.selection-inspector'), null, 'same-mode selection stays open');
		await clickButton(page, 'Grid');
		await page.waitForFunction(() => !document.querySelector('.selection-inspector'), {
			timeout: WAIT_MS
		});
		assert.equal(
			await page.evaluate(() => history.state?.['sveltekit:states']?.studioOverlay ?? null),
			null,
			'mode switch removes the inspector history entry'
		);
		await clickButton(page, 'Lineage');
		await page.waitForSelector('.lineage-world', { timeout: WAIT_MS });
		await page.waitForFunction(
			() =>
				new URL(location.href).searchParams.get('view') === 'images' &&
				[...document.querySelectorAll('button[aria-pressed="true"]')].some(
					(button) => button.textContent?.trim() === 'Lineage'
				),
			{ timeout: WAIT_MS }
		);
		await page.evaluate(
			() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
		);
		assert.equal(
			await page.$('.selection-inspector'),
			null,
			'a Lineage mode change does not restore its cleared selection hint'
		);
		assert.equal(await page.evaluate(() => window.__rootQueries.length), queryCount);
		const after = await page.evaluate(() => {
			const node = document.querySelector(`[data-lineage-node="${window.__modeNodeId}"]`);
			const rect = node.getBoundingClientRect();
			return {
				transform: document.querySelector('.lineage-world').style.transform,
				id: node.dataset.lineageNode,
				left: rect.left,
				top: rect.top
			};
		});
		assert.equal(after.id, before.id);
		assert.equal(after.transform, before.transform);
		assert.ok(Math.abs(after.left - before.left) < 4);
		assert.ok(Math.abs(after.top - before.top) < 4);
		await clickButton(page, 'Grid');
		await chooseImageModel(page, 'Other root model');
		const beforeChangedCriteria = await page.evaluate(() => window.__rootQueries.length);
		await clickButton(page, 'Lineage');
		await page.waitForFunction(
			(count) => window.__rootQueries.length === count + 1,
			{ timeout: WAIT_MS },
			beforeChangedCriteria
		);
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('model_id'), 'other-root-model');
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('state'), 'succeeded');
		await pause(250);
		assert.equal(await page.evaluate(() => window.__rootQueries.length), beforeChangedCriteria + 1);
		await page.evaluate(() => history.back());
		await page.waitForFunction(
			() => new URL(location.href).searchParams.get('view') === 'generate',
			{
				timeout: WAIT_MS
			}
		);
	} finally {
		await harness.close();
	}
});

test('an incomplete custom date keeps lineage search results and its match index', async () => {
	const matches = [0, 1].map((index) =>
		lineageGeneration(
			lineageJobId(1400 + index),
			lineageAssetId(1400 + index),
			`Needle root ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const searchQueries = [];
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?roots_only=true&limit=50': { body: matches },
		'/api/v1/generations?fields=ids&limit=5000': {
			body: (url) => {
				searchQueries.push(url.searchParams.toString());
				return { ids: matches.map((generation) => generation.id) };
			}
		},
		...Object.fromEntries(
			matches.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${matches[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.type('input[name="q"]', 'needle');
		await page.waitForFunction(() => document.body.textContent.includes('Matches: 2'), {
			timeout: WAIT_MS
		});
		await clickButton(page, 'Next match');
		const firstMatchTransform = await page.$eval('.lineage-world', (world) =>
			world.getAttribute('style')
		);
		const queriesBeforeInvalidDate = searchQueries.length;
		await chooseImageOption(page, 'Filter by creation date', 'Custom dates');
		await page.waitForSelector('#image-filter-from', { timeout: WAIT_MS });
		await page.$eval('#image-filter-from', (input) => {
			input.value = '2035-01-01';
			input.dispatchEvent(new Event('change', { bubbles: true }));
		});
		await page.waitForFunction(() => document.body.textContent.includes('Choose both dates'), {
			timeout: WAIT_MS
		});
		assert.equal(searchQueries.length, queriesBeforeInvalidDate);
		assert.match(
			await page.$eval('.lineage-section > p[role="status"]', (status) => status.textContent),
			/Matches: 2/
		);
		assert.equal((await button(page, 'Next match')).disabled, false);
		await clickButton(page, 'Next match');
		const secondMatchTransform = await page.$eval('.lineage-world', (world) =>
			world.getAttribute('style')
		);
		assert.notEqual(
			secondMatchTransform,
			firstMatchTransform,
			'the next match advances from the previous index after an incomplete date edit'
		);
	} finally {
		await harness.close();
	}
});

test('Images replacement keeps keyed tiles and inspector, sends sizes, and holds incomplete dates', async () => {
	const fixtures = imageGridFixtures(72);
	let releaseInitialPage;
	const initialPage = new Promise((resolvePage) => {
		releaseInitialPage = resolvePage;
	});
	for (const [index, row] of fixtures.rows.entries()) {
		row.assets[0].width = index < 36 ? 512 : 2048;
		row.assets[0].height = index < 36 ? 512 : 1024;
	}
	const multiOutput = lineageGeneration(
		lineageJobId(972),
		lineageAssetId(972),
		'Multi-output job with omitted expired master',
		new Date().toISOString()
	);
	multiOutput.expired = false;
	multiOutput.assets[0].width = 512;
	multiOutput.assets[0].height = 512;
	fixtures.rows.push(multiOutput);
	const requests = [];
	fixtures.answers['/api/v1/generations?limit=50'] = {
		body: async (url) => {
			await initialPage;
			requests.push(url.searchParams.toString());
			if (url.searchParams.has('size') || url.searchParams.has('action')) await pause(400);
			const size = url.searchParams.getAll('size');
			return fixtures.rows.filter((row) => {
				const dimension = Math.max(row.assets[0].width, row.assets[0].height);
				return (
					size.length === 0 ||
					(size.includes('2k') && dimension >= 1536 && dimension < 3072) ||
					(size.includes('2k') && row.id === multiOutput.id)
				);
			});
		}
	};
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await page.waitForSelector('.grid-scroller [role="status"]', { timeout: WAIT_MS });
		assert.match(
			await page.$eval('.grid-scroller [role="status"]', (status) => status.textContent),
			/Loading/
		);
		releaseInitialPage();
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 73, {
			timeout: WAIT_MS
		});
		const dimensions = await page.$$eval('[aria-label^="Filter by"]', (controls) =>
			controls.map((control) => ({
				height: getComputedStyle(control).height,
				fontSize: getComputedStyle(control).fontSize,
				fontWeight: getComputedStyle(control).fontWeight
			}))
		);
		assert.ok(dimensions.length >= 3);
		for (const style of dimensions) {
			assert.equal(style.height, '26px');
			assert.equal(style.fontSize, '12px');
			assert.equal(style.fontWeight, '500');
		}

		const selectedId = fixtures.rows[50].id;
		await page.$eval('.grid-scroller', (element) => (element.style.overflowAnchor = 'none'));
		await page.$eval(`[data-generation-id="${selectedId}"]`, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.evaluate((id) => {
			window.__gridContinuityNodes = {
				list: document.querySelector('.image-grid'),
				image: document.querySelector(`[data-generation-id="${id}"] img`),
				inspector: document.querySelector('.selection-inspector')
			};
		}, selectedId);
		const scroller = await page.$('.grid-scroller');
		assert.ok(scroller);
		await page.$eval(`[data-generation-id="${selectedId}"]`, (tile) =>
			tile.scrollIntoView({ block: 'center' })
		);
		const anchor = await page.$eval(
			'.grid-scroller',
			(scroller, firstTwoKId) => {
				const area = scroller.getBoundingClientRect();
				const tile = [...scroller.querySelectorAll('.grid-tile')].find((candidate) => {
					const rect = candidate.getBoundingClientRect();
					return (
						candidate.dataset.generationId >= firstTwoKId &&
						rect.bottom > area.top &&
						rect.top < area.bottom
					);
				});
				return tile && { id: tile.dataset.generationId, top: tile.getBoundingClientRect().top };
			},
			fixtures.rows[36].id
		);
		assert.ok(anchor);
		await chooseImageOption(page, 'Filter by image size', '2K (1536-3071 px)');
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'true',
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.evaluate(
				() => document.querySelector('.image-grid') === window.__gridContinuityNodes.list
			),
			true
		);
		assert.equal(
			await page.evaluate(
				(id) =>
					document.querySelector(`[data-generation-id="${id}"] img`) ===
					window.__gridContinuityNodes.image,
				selectedId
			),
			true
		);
		assert.equal(
			await page.evaluate(
				() =>
					document.querySelector('.selection-inspector') === window.__gridContinuityNodes.inspector
			),
			true
		);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.querySelector('[role="status"]')),
			null,
			'replacement does not show a loading message'
		);
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 37, {
			timeout: WAIT_MS
		});
		assert.notEqual(
			await page.$(`[data-generation-id="${multiOutput.id}"]`),
			null,
			'the current server page keeps a 2K match whose expired master is not serialized'
		);
		assert.ok(requests.some((query) => new URLSearchParams(query).getAll('size').includes('2k')));
		assert.equal(
			await page.evaluate(
				() => document.querySelector('.image-grid') === window.__gridContinuityNodes.list
			),
			true
		);
		assert.ok(
			Math.abs(
				(await page.$eval(
					`[data-generation-id="${anchor.id}"]`,
					(tile) => tile.getBoundingClientRect().top
				)) - anchor.top
			) <= 1,
			'the visible common tile keeps its screen position'
		);
		assert.equal(
			await page.evaluate(
				() =>
					document.querySelector('.selection-inspector') === window.__gridContinuityNodes.inspector
			),
			true
		);

		await page.click('[aria-label="Clear model, size, and date filters"]');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 73, {
			timeout: WAIT_MS
		});
		assert.equal(
			await page.evaluate(
				(id) =>
					document.querySelector(`[data-generation-id="${id}"] img`) ===
					window.__gridContinuityNodes.image,
				selectedId
			),
			true,
			'clearing a filter reuses the common image node'
		);
		const scrollingAnchorId = fixtures.rows[36].id;
		await page.$eval(`[data-generation-id="${scrollingAnchorId}"]`, (tile) =>
			tile.scrollIntoView({ block: 'start' })
		);
		await page.$eval('.grid-scroller', (element) => (element.style.overflowAnchor = 'none'));
		await chooseImageOption(page, 'Filter by image size', '2K (1536-3071 px)');
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'true',
			{ timeout: WAIT_MS }
		);
		const scrollBeforeWheel = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		const scrollerRect = await scroller.boundingBox();
		assert.ok(scrollerRect);
		await page.mouse.move(scrollerRect.x + 40, scrollerRect.y + 40);
		await page.mouse.wheel({ deltaY: 120 });
		await page.waitForFunction(
			(previous) => document.querySelector('.grid-scroller').scrollTop > previous,
			{},
			scrollBeforeWheel
		);
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		const wheelPreservedScroll = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		assert.ok(wheelPreservedScroll > 0);
		assert.ok(
			(await page.$eval(
				`[data-generation-id="${scrollingAnchorId}"]`,
				(tile) => tile.getBoundingClientRect().top
			)) <
				scrollerRect.y - 20,
			'a wheel scroll during replacement is not reset to the saved tile anchor'
		);
		await page.click('[aria-label="Clear model, size, and date filters"]');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 73, {
			timeout: WAIT_MS
		});
		await page.$eval('.grid-scroller', (element) => (element.scrollTop = 200));
		await page.evaluate(() => {
			const generated = [...document.querySelectorAll('button[aria-pressed]')].find(
				(button) => button.textContent?.trim() === 'Generated'
			);
			if (!generated) throw new Error('generated action filter not found');
			generated.click();
		});
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'true',
			{ timeout: WAIT_MS }
		);
		const dragStart = await visibleGridTile(page);
		assert.ok(dragStart);
		const scrollBeforeDrag = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		await page.mouse.move(dragStart.x, dragStart.y);
		await page.mouse.down();
		await page.mouse.move(dragStart.x, dragStart.y - 100, { steps: 5 });
		await page.mouse.up();
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.ok(
			(await page.$eval('.grid-scroller', (element) => element.scrollTop)) > scrollBeforeDrag + 20,
			'a drag scroll during replacement is not reset to the saved tile anchor'
		);
		await clickButton(page, 'All');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 73, {
			timeout: WAIT_MS
		});
		await page.$eval('.grid-scroller', (element) => (element.scrollTop = 200));
		await clickButton(page, 'Generated');
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'true',
			{ timeout: WAIT_MS }
		);
		const touchScrollTop = await page.$eval('.grid-scroller', (element) => {
			const rect = element.getBoundingClientRect();
			element.dispatchEvent(
				new PointerEvent('pointerdown', {
					bubbles: true,
					pointerType: 'touch',
					pointerId: 71,
					button: 0,
					clientX: rect.left + 40,
					clientY: rect.top + 40
				})
			);
			element.scrollTop += 100;
			return element.scrollTop;
		});
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			touchScrollTop,
			'an ignored touch press cancels the pending scroll anchor'
		);
		await clickButton(page, 'All');
		await page.waitForFunction(
			() => document.querySelector('.image-grid')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		const requestsBeforeCustomDate = requests.length;
		await chooseImageOption(page, 'Filter by creation date', 'Custom dates');
		await page.waitForSelector('#image-filter-from');
		await page.$eval('#image-filter-from', (input) => {
			input.value = '2035-01-01';
			input.dispatchEvent(new Event('change', { bubbles: true }));
		});
		await page.waitForFunction(() => document.body.textContent.includes('Choose both dates'));
		await pause(250);
		assert.equal(requests.length, requestsBeforeCustomDate, 'an incomplete range sends no query');
		assert.equal(await page.$$('.grid-tile').then((tiles) => tiles.length), 73);
		await clickButton(page, 'All');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 73, {
			timeout: WAIT_MS
		});
		const allQuery = new URLSearchParams(requests.at(-1));
		assert.equal(allQuery.has('size'), false);
		assert.equal(allQuery.has('created_from'), false);
	} finally {
		releaseInitialPage();
		await harness.close();
	}
});

test('grid ignores a late response from a replaced date query', async () => {
	const fixtures = imageGridFixtures(3);
	fixtures.rows[2].created_at = new Date(Date.now() - 60 * 60 * 1000).toISOString();
	let delayedDateResponse = false;
	const dateQueries = [];
	fixtures.answers['/api/v1/generations?limit=50'] = {
		body: async (url) => {
			if (url.searchParams.has('created_from')) {
				dateQueries.push(url.searchParams.toString());
				if (!delayedDateResponse) {
					delayedDateResponse = true;
					await pause(450);
					return [fixtures.rows[0], fixtures.rows[2]];
				}
				return [fixtures.rows[0], fixtures.rows[1]];
			}
			return fixtures.rows;
		}
	};
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__dateQueries = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (
				url.pathname === '/api/v1/generations' &&
				url.searchParams.has('created_from') &&
				!url.searchParams.has('roots_only')
			) {
				window.__dateQueries.push(url.searchParams.toString());
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 3, {
			timeout: WAIT_MS
		});
		await chooseImageOption(page, 'Filter by creation date', 'Last 7 days');
		await page.waitForFunction(() => document.querySelector('[aria-busy="true"]'));
		await chooseImageOption(page, 'Filter by creation date', 'Today');
		await pause(300);
		const dateQueryState = await page.evaluate(() => ({
			queries: window.__dateQueries,
			label: document.querySelector('[aria-label="Filter by creation date"]')?.textContent,
			busy: document.querySelector('.grid-scroller')?.getAttribute('aria-busy')
		}));
		assert.equal(dateQueryState.queries.length, 2, JSON.stringify(dateQueryState));
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 2, {
			timeout: WAIT_MS
		});
		await pause(500);
		assert.deepEqual(
			await page.$$eval('.grid-tile', (tiles) => tiles.map((tile) => tile.dataset.generationId)),
			[fixtures.rows[0].id, fixtures.rows[1].id]
		);
		assert.equal(dateQueries.length, 2, 'both distinct filter requests reached the fixture server');
	} finally {
		await harness.close();
	}
});

test('server classified historical rows stay visible when their model is unavailable locally', async () => {
	const fixtures = imageGridFixtures(2);
	fixtures.rows[0].model_id = 'removed-history-model';
	fixtures.rows[0].source_asset_id = lineageAssetId(999);
	fixtures.rows[1].source_asset_id = lineageAssetId(998);
	fixtures.answers['/api/v1/generations/models'] = { body: { models: [MODEL] } };
	fixtures.answers[`/api/v1/generations/${fixtures.rows[0].id}/lineage`] = {
		body: { ancestors: [], children: [], descendant_count: 0, descendants_truncated: false }
	};
	const actionQueries = [];
	fixtures.answers['/api/v1/generations?limit=50'] = {
		body: (url) => {
			if (url.searchParams.get('action') === 'upscale')
				actionQueries.push(url.searchParams.toString());
			return fixtures.rows;
		}
	};
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__actionQueries = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('action')) {
				window.__actionQueries.push(url.searchParams.toString());
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 2, {
			timeout: WAIT_MS
		});
		await page.evaluate(() => {
			const upscale = [...document.querySelectorAll('button[aria-pressed]')].find(
				(candidate) => candidate.textContent?.trim() === 'Upscaled'
			);
			if (!upscale) throw new Error('upscale filter not found');
			upscale.click();
		});
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button[aria-pressed]')].some(
					(button) =>
						button.textContent?.trim() === 'Upscaled' &&
						button.getAttribute('aria-pressed') === 'true'
				),
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(() => window.__actionQueries.length === 1, { timeout: WAIT_MS });
		await page.waitForFunction(
			() => document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.equal(new URLSearchParams(actionQueries.at(-1)).get('action'), 'upscale');
		assert.equal(
			await page.$$('.grid-tile').then((tiles) => tiles.length),
			2,
			'server membership wins over absent models and narrowed public model capabilities'
		);
		await page.$eval('.grid-tile', (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
	} finally {
		await harness.close();
	}
});

test('lineage replaces roots atomically after paging beyond fifty', async () => {
	const roots = Array.from({ length: 60 }, (_, index) => {
		const root = lineageGeneration(
			lineageJobId(900 + index),
			lineageAssetId(900 + index),
			`Paged root ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		);
		if (index >= 20) root.model_id = 'other-history-smoke';
		return root;
	});
	const rootQueries = [];
	const answers = {
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: async (url) => {
				rootQueries.push(url.searchParams.toString());
				const cursor = url.searchParams.get('cursor');
				if (url.searchParams.get('model_id') === 'other-history-smoke') {
					await pause(350);
					return roots.filter((root) => root.model_id === 'other-history-smoke');
				}
				if (cursor !== null) return roots.slice(50);
				return roots.slice(0, 50);
			}
		},
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await launchStudio(
		'en',
		() => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			window.__rootPageCursors = [];
			window.__rootQueries = [];
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
					window.__rootPageCursors.push(url.searchParams.get('cursor'));
					window.__rootQueries.push(url.searchParams.toString());
				}
				return originalFetch(input, init);
			};
		},
		answers,
		'domcontentloaded',
		'/app?view=images'
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		const commonNodeId = roots[20].assets[0].id;
		await page.waitForSelector(`[data-lineage-node="${commonNodeId}"]`, { timeout: WAIT_MS });
		await page.evaluate((id) => {
			window.__savedRootNode = document.querySelector(`[data-lineage-node="${id}"]`);
		}, commonNodeId);
		await page.focus('.lineage-viewport');
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowDown');
		await page.waitForFunction(
			() => window.__rootPageCursors.filter((cursor) => cursor !== null).length > 0,
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(() => window.__rootPageCursors.length >= 2, { timeout: WAIT_MS });
		for (let step = 0; step < 40; step += 1) await page.keyboard.press('ArrowUp');
		await page.waitForSelector(`[data-lineage-node="${commonNodeId}"]`, { timeout: WAIT_MS });
		await page.evaluate((id) => {
			window.__savedRootNode = document.querySelector(`[data-lineage-node="${id}"]`);
		}, commonNodeId);
		await chooseImageModel(page, 'Other history model');
		await page.waitForFunction(
			() => window.__rootQueries.some((query) => new URLSearchParams(query).has('model_id')),
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.evaluate(
				(id) => document.querySelector(`[data-lineage-node="${id}"]`) === window.__savedRootNode,
				commonNodeId
			),
			true,
			'the keyed common root stays mounted while its replacement is pending'
		);
		await pause(450);
		assert.equal(
			await page.evaluate(
				(id) => document.querySelector(`[data-lineage-node="${id}"]`) === window.__savedRootNode,
				commonNodeId
			),
			true,
			'the keyed common root remains after replacement'
		);
		assert.equal(rootQueries.filter((query) => new URLSearchParams(query).has('cursor')).length, 1);
	} finally {
		await harness.close();
	}
});

test('grid mouse drag scrolls without selecting and preserves click, cancel, reduced motion, and wheel', async () => {
	const fixtures = imageGridFixtures(120);
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 120, {
			timeout: WAIT_MS
		});
		const scroller = await page.$('.grid-scroller');
		assert.ok(scroller);
		const tile = await page.$('.grid-tile');
		const rect = await tile.boundingBox();
		assert.ok(rect);
		await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
		await page.mouse.down();
		await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2 - 220, { steps: 8 });
		assert.equal(
			await page.$eval('.grid-scroller .grid-tile', (element) => getComputedStyle(element).cursor),
			'grabbing'
		);
		await page.$eval('.grid-scroller', (scroller) => {
			const tile = scroller.querySelector('.grid-tile');
			scroller.addEventListener(
				'pointerup',
				() => {
					const compatibilityClick = new MouseEvent('click', {
						bubbles: true,
						cancelable: true,
						detail: 1
					});
					tile.dispatchEvent(compatibilityClick);
					window.__dragClickPrevented = compatibilityClick.defaultPrevented;
				},
				{ once: true }
			);
		});
		await page.mouse.up();
		assert.equal(await page.evaluate(() => window.__dragClickPrevented), true);
		await page.waitForFunction(() => document.querySelector('.grid-scroller').scrollTop > 150);
		const scrollAfterDrag = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		assert.ok(scrollAfterDrag > 0);
		assert.equal(
			await page.$('.selection-inspector'),
			null,
			'a tile drag does not select the tile'
		);
		const rightClickTarget = await page.evaluate(() => {
			const scroller = document.querySelector('.grid-scroller').getBoundingClientRect();
			const tile = [...document.querySelectorAll('.grid-tile')].find((candidate) => {
				const rect = candidate.getBoundingClientRect();
				return rect.bottom > scroller.top && rect.top < scroller.bottom;
			});
			const rect = tile.getBoundingClientRect();
			return { id: tile.dataset.generationId, x: rect.left + 20, y: rect.top + 20 };
		});
		await page.mouse.click(rightClickTarget.x, rightClickTarget.y, { button: 'right' });
		await page.$eval(`[data-generation-id="${rightClickTarget.id}"]`, (button) => button.focus());
		await page.keyboard.press('Enter');
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.click('.selection-inspector button[aria-label="Close selected image"]');
		await page.waitForFunction(() => !document.querySelector('.selection-inspector'));
		await pause(1200);

		const visibleTile = await visibleGridTile(page);
		assert.ok(visibleTile);
		await page.mouse.click(visibleTile.x, visibleTile.y);
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.$eval('.grid-scroller', (element) => (element.scrollTop = 300));
		const outsideStart = await visibleGridTile(page);
		assert.ok(outsideStart);
		const scrollerRect = await scroller.boundingBox();
		const inspectorRect = await page.$('.selection-inspector').then((node) => node.boundingBox());
		assert.ok(scrollerRect && inspectorRect);
		const retainedScrollTop = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		await page.mouse.move(outsideStart.x, outsideStart.y);
		await page.mouse.down();
		await page.mouse.move(inspectorRect.x + 20, outsideStart.y, { steps: 4 });
		await page.mouse.up();
		await page.mouse.move(scrollerRect.x + 30, scrollerRect.y + 30);
		await page.mouse.move(scrollerRect.x + 30, scrollerRect.y + 120);
		assert.equal(
			await page.$eval('.grid-scroller', (element) =>
				element.classList.contains('is-drag-scrolling')
			),
			false
		);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			retainedScrollTop,
			'a release outside the scroller before the vertical threshold ends the gesture'
		);
		await page.click('.selection-inspector button[aria-label="Close selected image"]');
		await page.waitForFunction(() => !document.querySelector('.selection-inspector'));

		const clickTile = await visibleGridTile(page);
		assert.ok(clickTile);
		await page.mouse.move(clickTile.x + 10, clickTile.y + 10);
		await page.mouse.down();
		await page.mouse.move(clickTile.x + 10, clickTile.y + 15);
		await page.mouse.up();
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.click('.selection-inspector button[aria-label="Close selected image"]');
		await page.waitForFunction(() => !document.querySelector('.selection-inspector'));

		const client = await page.createCDPSession();
		await client.send('Emulation.setEmulatedMedia', {
			features: [{ name: 'prefers-reduced-motion', value: 'reduce' }]
		});
		await page.$eval('.grid-scroller', (element) => (element.scrollTop = 300));
		const reducedTile = await visibleGridTile(page);
		assert.ok(reducedTile);
		await page.mouse.move(reducedTile.x + 5, reducedTile.y + 5);
		await page.mouse.down();
		await page.mouse.move(reducedTile.x + 5, reducedTile.y - 135, { steps: 5 });
		assert.equal(
			await page.$eval('.grid-scroller', (element) =>
				element.classList.contains('is-drag-scrolling')
			),
			true
		);
		await page.mouse.up();
		const afterRelease = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		assert.ok(afterRelease > 400);
		assert.ok(
			await page.$eval(
				'.grid-scroller',
				(element) => element.scrollHeight - element.clientHeight - element.scrollTop > 300
			)
		);
		await pause(160);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			afterRelease,
			'reduced motion disables coast after the pointer is released'
		);
		await client.send('Emulation.setEmulatedMedia', { features: [] });

		const cancelTile = await visibleGridTile(page);
		assert.ok(cancelTile);
		await page.mouse.move(cancelTile.x + 10, cancelTile.y + 10);
		await page.mouse.down();
		await page.mouse.move(cancelTile.x + 10, cancelTile.y - 90, { steps: 4 });
		await page.$eval('.grid-scroller', (element) =>
			element.dispatchEvent(
				new PointerEvent('pointercancel', { bubbles: true, pointerId: 1, pointerType: 'mouse' })
			)
		);
		await page.mouse.up();
		const afterCancel = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		await pause(180);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			afterCancel,
			'pointer cancellation stops movement without coast'
		);

		const lostCaptureTile = await visibleGridTile(page);
		assert.ok(lostCaptureTile);
		await page.$eval('.grid-scroller', (element) => {
			element.addEventListener('pointermove', (event) => {
				if (event.buttons === 1) window.__activeDragPointerId = event.pointerId;
			});
		});
		await page.mouse.move(lostCaptureTile.x, lostCaptureTile.y);
		await page.mouse.down();
		await page.mouse.move(lostCaptureTile.x, lostCaptureTile.y - 90, { steps: 4 });
		const lostCapturePointerId = await page.evaluate(() => window.__activeDragPointerId);
		assert.equal(typeof lostCapturePointerId, 'number');
		assert.equal(
			await page.$eval(
				'.grid-scroller',
				(element, pointerId) => element.hasPointerCapture(pointerId),
				lostCapturePointerId
			),
			true
		);
		await page.$eval(
			'.grid-scroller',
			(element, pointerId) => {
				element.dispatchEvent(
					new PointerEvent('lostpointercapture', {
						bubbles: true,
						pointerId,
						pointerType: 'mouse'
					})
				);
			},
			lostCapturePointerId
		);
		await page.waitForFunction(
			() => !document.querySelector('.grid-scroller').classList.contains('is-drag-scrolling'),
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.$eval('.grid-scroller', (element) =>
				element.classList.contains('is-drag-scrolling')
			),
			false
		);
		await page.mouse.up();
		const afterLostCapture = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		await pause(160);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			afterLostCapture,
			'lost pointer capture ends the drag without coast'
		);

		const blurTile = await visibleGridTile(page);
		assert.ok(blurTile);
		await page.mouse.move(blurTile.x, blurTile.y);
		await page.mouse.down();
		await page.mouse.move(blurTile.x, blurTile.y - 90, { steps: 4 });
		await page.evaluate(() => window.dispatchEvent(new Event('blur')));
		assert.equal(
			await page.$eval('.grid-scroller', (element) =>
				element.classList.contains('is-drag-scrolling')
			),
			false
		);
		await page.mouse.up();
		const afterBlur = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		await pause(160);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			afterBlur,
			'window blur ends the drag without coast'
		);

		const keyTile = await visibleGridTile(page);
		assert.ok(keyTile);
		await page.mouse.move(keyTile.x, keyTile.y);
		await page.mouse.down();
		await page.mouse.move(keyTile.x, keyTile.y - 90, { steps: 4 });
		await page.mouse.up();
		await page.$eval(`[data-generation-id="${keyTile.id}"]`, (tile) => tile.focus());
		await page.keyboard.press('Escape');
		const afterKey = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		await pause(160);
		assert.equal(
			await page.$eval('.grid-scroller', (element) => element.scrollTop),
			afterKey,
			'keyboard input stops coast'
		);

		await page.mouse.move(scrollerRect.x + 40, scrollerRect.y + 40);
		const beforeWheel = await page.$eval('.grid-scroller', (element) => {
			element.addEventListener('wheel', () => (window.__sawGridWheel = true), { once: true });
			return element.scrollTop;
		});
		await page.mouse.wheel({ deltaY: 160 });
		await page.waitForFunction(
			(previous) => document.querySelector('.grid-scroller').scrollTop > previous,
			{},
			beforeWheel
		);
	} finally {
		await harness.close();
	}
});

test('un-starring a filtered grid result removes it from that server page', async () => {
	const fixtures = imageGridFixtures(6);
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await clickButton(page, 'Starred');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 6, {
			timeout: WAIT_MS
		});
		await page.$eval('.grid-tile', (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await clickButton(page, 'Unstar');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 5, {
			timeout: WAIT_MS
		});
		assert.equal(await page.$('.selection-inspector'), null);
	} finally {
		await harness.close();
	}
});

test('filtered older grid images open in the inspector and close with Back or a mobile swipe', async () => {
	const fixtures = imageGridFixtures(51);
	fixtures.rows[50].params.prompt = 'Older filtered image beyond the first page';
	fixtures.rows[50].created_at = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
	fixtures.rows[50].assets[0].thumbnail_url = '/api/v1/assets/held-thumbnail.png';
	let heldThumbnailRequest = null;
	fixtures.answers['/api/v1/generations?limit=50'] = {
		body: (url) =>
			url.searchParams.has('cursor') ? fixtures.rows.slice(50) : fixtures.rows.slice(0, 50)
	};
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await page.setRequestInterception(true);
		page.on('request', (request) => {
			if (request.url().endsWith('/api/v1/assets/held-thumbnail.png')) {
				heldThumbnailRequest = request;
				return;
			}
			void request.continue();
		});
		await chooseImageOption(page, 'Filter by creation date', 'Last 7 days');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'More matching results');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 51, {
			timeout: WAIT_MS
		});
		const olderTile = '[data-generation-id="' + fixtures.rows[50].id + '"]';
		assert.notEqual(await page.$(olderTile), null);
		await page.$eval(olderTile, (tile) => tile.scrollIntoView({ block: 'center' }));
		await page.waitForFunction(
			(selector) => {
				const image = document.querySelector(selector + ' img');
				return (
					image?.getAttribute('src') === '/api/v1/assets/held-thumbnail.png' && !image.complete
				);
			},
			{ timeout: WAIT_MS },
			olderTile
		);
		assert.match(
			await page.$eval(olderTile, (tile) => getComputedStyle(tile).backgroundImage),
			/linear-gradient/
		);
		heldThumbnailRequest ??= await page.waitForRequest(
			(request) => new URL(request.url()).pathname === '/api/v1/assets/held-thumbnail.png',
			{ timeout: WAIT_MS }
		);
		await heldThumbnailRequest.abort();
		await page.$eval(olderTile, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.match(
			await page.$eval('.selection-inspector', (panel) => panel.textContent),
			/Older filtered image beyond the first page/
		);

		await page.evaluate(() => history.back());
		await page.waitForFunction(() => !document.querySelector('.selection-inspector'), {
			timeout: WAIT_MS
		});
		assert.equal(await page.$$('.grid-tile').then((tiles) => tiles.length), 51);

		await page.setViewport({ width: 390, height: 844 });
		await page.$eval(olderTile, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		const panel = await page.$('.selection-inspector');
		const bounds = await panel.boundingBox();
		assert.ok(bounds);
		const touch = await page.createCDPSession();
		await touch.send('Emulation.setTouchEmulationEnabled', { enabled: true });
		await touch.send('Input.dispatchTouchEvent', {
			type: 'touchStart',
			touchPoints: [{ x: bounds.x + 30, y: bounds.y + 120, id: 1 }]
		});
		await touch.send('Input.dispatchTouchEvent', {
			type: 'touchMove',
			touchPoints: [{ x: bounds.x + bounds.width * 0.65, y: bounds.y + 120, id: 1 }]
		});
		await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
		await page.waitForFunction(() => !document.querySelector('.selection-inspector'), {
			timeout: WAIT_MS
		});
		await touch.detach();
	} finally {
		await harness.close();
	}
});

test('Unstar Undo restores an image while Starred and action filters are active', async () => {
	const fixtures = imageGridFixtures(51);
	const baseTime = Date.parse(fixtures.rows[0].created_at);
	for (const [index, row] of fixtures.rows.entries()) {
		const timestamp = new Date(baseTime - (index + 1) * 1000).toISOString();
		row.created_at = timestamp;
		row.starred_at = timestamp;
	}
	const olderRow = fixtures.rows[50];
	const historyPageIds = [];
	const gridPages = [];
	const getGenerationPage = (url) => {
		const starred = url.searchParams.get('starred') === 'true';
		const action = url.searchParams.get('action');
		const matching = fixtures.rows
			.filter((row) => row.state === (url.searchParams.get('state') ?? row.state))
			.filter((row) => !starred || row.starred_at !== null)
			.filter((row) => action !== 'generate' || row.source_asset_id === null)
			.sort((left, right) => {
				const leftTime = Date.parse(starred ? left.starred_at : left.created_at);
				const rightTime = Date.parse(starred ? right.starred_at : right.created_at);
				return rightTime - leftTime || right.id.localeCompare(left.id);
			});
		const cursor = url.searchParams.get('cursor');
		const cursorIndex = cursor === null ? -1 : matching.findIndex((row) => row.id === cursor);
		assert.equal(
			cursor === null || cursorIndex >= 0,
			true,
			'cursor belongs to this filtered result'
		);
		const limit = Number(url.searchParams.get('limit') ?? 50);
		const page = matching.slice(cursorIndex + 1, cursorIndex + 1 + limit);
		if (url.searchParams.has('state')) {
			gridPages.push({
				cursor,
				starred,
				action,
				state: url.searchParams.get('state'),
				ids: page.map((row) => row.id)
			});
		} else if (!starred) {
			historyPageIds.splice(0, historyPageIds.length, ...page.map((row) => row.id));
		}
		return page;
	};
	fixtures.answers['/api/v1/generations?limit=50'] = { body: getGenerationPage };
	fixtures.answers['/api/v1/generations?limit=50&starred=true'] = {
		body: getGenerationPage
	};
	fixtures.answers['/api/v1/generations?starred=true&limit=200'] = {
		body: () =>
			fixtures.rows
				.filter((row) => row.starred_at !== null)
				.sort(
					(left, right) =>
						Date.parse(right.starred_at) - Date.parse(left.starred_at) ||
						right.id.localeCompare(left.id)
				)
	};
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		assert.equal(historyPageIds.length, 50);
		assert.equal(
			historyPageIds.includes(olderRow.id),
			false,
			'the target is outside shared recent history'
		);
		await clickButton(page, 'Starred');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'Generated');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button[aria-pressed]')].some(
					(button) =>
						button.textContent?.trim() === 'Generated' &&
						button.getAttribute('aria-pressed') === 'true'
				),
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		assert.deepEqual(
			gridPages.at(-1).ids,
			fixtures.rows.slice(0, 50).map((row) => row.id)
		);
		await clickButton(page, 'More matching results');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 51, {
			timeout: WAIT_MS
		});
		assert.equal(gridPages.at(-1).cursor, fixtures.rows[49].id);
		assert.deepEqual(gridPages.at(-1).ids, [olderRow.id]);
		const id = olderRow.id;
		await page.$eval('[data-generation-id="' + id + '"]', (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await clickButton(page, 'Unstar');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		assert.equal(olderRow.starred_at, null, 'DELETE removes the favorite in the server fixture');
		const favoritesAfterUnstar = await page.evaluate(async () =>
			(await fetch('/api/v1/generations?starred=true&limit=200')).json()
		);
		assert.equal(
			favoritesAfterUnstar.some((row) => row.id === id),
			false
		);
		const pagesBeforeUndo = gridPages.length;
		await clickButton(page, 'Undo');
		await page.waitForFunction(
			(id) =>
				document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false' &&
				document.querySelector('.grid-tile')?.dataset.generationId === id,
			{ timeout: WAIT_MS },
			id
		);
		assert.equal(gridPages.length, pagesBeforeUndo + 1, 'Undo reloads the first server page');
		assert.equal(await page.$$('.grid-tile').then((tiles) => tiles.length), 50);
		assert.equal(gridPages.at(-1).cursor, null);
		assert.equal(gridPages.at(-1).starred, true);
		assert.equal(gridPages.at(-1).action, 'generate');
		assert.equal(gridPages.at(-1).state, 'succeeded');
		assert.equal(gridPages.at(-1).ids[0], id, 'the restored favorite sorts to the newest position');
		assert.ok(Date.parse(olderRow.starred_at) > baseTime - 1000);
		assert.equal(await page.$eval('.grid-tile', (tile) => tile.dataset.generationId), id);
		const favoritesAfterUndo = await page.evaluate(async () =>
			(await fetch('/api/v1/generations?starred=true&limit=200')).json()
		);
		assert.equal(favoritesAfterUndo[0].id, id);
		assert.equal(
			await page.$eval(
				'[data-generation-id="' + id + '"]',
				(tile) => tile.querySelector('.grid-star') !== null
			),
			true
		);
	} finally {
		await harness.close();
	}
});

test('un-starring the cursor row moves the next page cursor to the last remaining favorite', async () => {
	const fixtures = imageGridFixtures(50);
	fixtures.answers['/api/v1/generations?limit=50&starred=true'].body = (url) =>
		url.searchParams.has('cursor') ? [] : fixtures.rows;
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__gridCursors = [];
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('starred')) {
				window.__gridCursors.push(url.searchParams.get('cursor'));
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await clickButton(page, 'Starred');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		await page.$$eval('.grid-tile', (tiles) => tiles.at(-1)?.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await clickButton(page, 'Unstar');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 49, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'More matching results');
		await page.waitForFunction(() => window.__gridCursors.some((cursor) => cursor !== null), {
			timeout: WAIT_MS
		});
		assert.equal(
			await page.evaluate(() => window.__gridCursors.at(-1)),
			fixtures.rows.at(-2).id,
			'the next page uses the last remaining starred server row as its cursor'
		);
	} finally {
		await harness.close();
	}
});

test('a refused un-star keeps the server result and its inspector visible', async () => {
	const fixtures = imageGridFixtures(6);
	fixtures.answers[`/api/v1/generations/${fixtures.rows[0].id}/star`] = {
		status: 500,
		body: { detail: 'star save failed' }
	};
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await clickButton(page, 'Starred');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 6, {
			timeout: WAIT_MS
		});
		await page.$eval('.grid-tile', (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await clickButton(page, 'Unstar');
		await page.waitForFunction(
			() => document.body.innerText.includes('The favorite could not be saved.'),
			{ timeout: WAIT_MS }
		);
		assert.equal(await page.$$('.grid-tile').then((tiles) => tiles.length), 6);
		assert.notEqual(await page.$('.selection-inspector'), null);
	} finally {
		await harness.close();
	}
});

test('the Images inspector shows execution time when it is present, including zero', async () => {
	const fixtures = imageGridFixtures(3);
	fixtures.rows[0].gpu_ms = 1500;
	fixtures.rows[1].gpu_ms = 0;
	fixtures.rows[2].gpu_ms = null;
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 3, {
			timeout: WAIT_MS
		});
		const gpuTimeFor = async (title) => {
			await page.$eval(`.grid-tile[title="${title}"]`, (tile) => tile.click());
			await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
			return page.$eval('.selection-inspector dl', (details) => {
				const label = [...details.querySelectorAll('dt')].find(
					(item) => item.textContent?.trim() === 'GPU time'
				);
				return label?.parentElement?.querySelector('dd')?.textContent?.trim() ?? null;
			});
		};
		assert.equal(await gpuTimeFor('Image filter result 1'), '1.5 s');
		assert.equal(await gpuTimeFor('Image filter result 2'), '0 ms');
		assert.equal(await gpuTimeFor('Image filter result 3'), null);
	} finally {
		await harness.close();
	}
});

test('changing the custom date filter clears an inspector for an excluded row', async () => {
	const fixtures = imageGridFixtures();
	fixtures.answers['/api/v1/generations?limit=50'].body = (url) =>
		url.searchParams.has('created_from') ? [] : fixtures.rows;
	const harness = await openImagesGrid(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 1, {
			timeout: WAIT_MS
		});
		await page.$eval('.grid-tile', (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await chooseImageOption(page, 'Filter by creation date', 'Custom dates');
		await page.waitForFunction(() => document.querySelectorAll('input[type="date"]').length === 2, {
			timeout: WAIT_MS
		});
		for (const id of ['image-filter-from', 'image-filter-through']) {
			await page.$eval(`#${id}`, (input) => {
				input.value = '2035-01-01';
				input.dispatchEvent(new Event('change', { bubbles: true }));
			});
		}
		await page.waitForFunction(
			() =>
				document.querySelectorAll('.grid-tile').length === 0 &&
				!document.querySelector('.selection-inspector'),
			{ timeout: WAIT_MS }
		);
	} finally {
		await harness.close();
	}
});

test('filtered lineage roots retain saved offsets until the filter is cleared', async () => {
	const root = lineageGeneration(
		LINEAGE_ROOT_JOB,
		lineageAssetId(1),
		'Offset root',
		new Date().toISOString()
	);
	root.expired = true;
	root.assets = [];
	const offset = { x: 137, y: -82 };
	const answers = {
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => (url.searchParams.get('model_id') === 'other-history-smoke' ? [] : [root])
		}
	};
	const harness = await launchStudio(
		'en',
		() => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			localStorage.setItem(
				'potocolom-view-v1:local:lineage-offsets',
				JSON.stringify({
					version: 1,
					section: 'lineage-offsets',
					value: { '13000000-0000-4000-8000-000000000001': { x: 137, y: -82 } }
				})
			);
		},
		answers,
		'domcontentloaded',
		'/app?view=images'
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${root.id}"]`, { timeout: WAIT_MS });
		const savedOffset = { [root.id]: offset };
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			savedOffset
		);
		await chooseImageModel(page, 'Other history model');
		await page.waitForFunction(
			() => !document.querySelector('[data-lineage-node="13000000-0000-4000-8000-000000000001"]'),
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(() => document.body.textContent.includes('No matching images.'), {
			timeout: WAIT_MS
		});
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			savedOffset,
			'the filtered empty forest keeps the saved root offset'
		);
		await page.click('[aria-label="Clear model, size, and date filters"]');
		await page.waitForSelector(`[data-lineage-node="${root.id}"]`, { timeout: WAIT_MS });
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			savedOffset,
			'clearing the filter restores the root without changing its offset'
		);
	} finally {
		await harness.close();
	}
});

test('clearing a Lineage filter in Grid keeps offsets for roots outside the cached subset', async () => {
	const roots = [
		lineageGeneration(
			lineageJobId(2050),
			lineageAssetId(2050),
			'Offset A',
			new Date(Date.now() - 60_000).toISOString()
		),
		lineageGeneration(
			lineageJobId(2051),
			lineageAssetId(2051),
			'Offset B',
			new Date(Date.now() - 100_000).toISOString()
		)
	];
	roots[1].model_id = 'other-history-smoke';
	const recentHistory = Array.from({ length: 49 }, (_, index) => {
		const generation = lineageGeneration(
			lineageJobId(2100 + index),
			null,
			`Recent failed history row ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		);
		generation.state = 'failed';
		return generation;
	});
	const offsets = {
		[roots[0].id]: { x: 137, y: -82 },
		[roots[1].id]: { x: -211, y: 93 }
	};
	const rootQueries = [];
	const answers = {
		'/api/v1/generations?limit=50': {
			body: (url) =>
				url.searchParams.get('state') === 'succeeded' ? roots : [...recentHistory, roots[0]]
		},
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				const modelId = url.searchParams.get('model_id');
				return modelId === null ? roots : roots.filter((root) => root.model_id === modelId);
			}
		},
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await openImagesGrid(
		answers,
		(savedOffsets) => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			localStorage.setItem(
				'potocolom-view-v1:local:lineage-offsets',
				JSON.stringify({ version: 1, section: 'lineage-offsets', value: savedOffsets })
			);
			window.__offsetRootLifecycle = { queries: [], completed: 0 };
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
					window.__offsetRootLifecycle.queries.push(url.searchParams.toString());
					return originalFetch(input, init).then((response) => {
						window.__offsetRootLifecycle.completed += 1;
						return response;
					});
				}
				return originalFetch(input, init);
			};
		},
		[offsets]
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(() => window.__offsetRootLifecycle.queries.length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__offsetRootLifecycle.completed === 2, {
			timeout: WAIT_MS
		});
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('model_id'), MODEL.id);
		await clickButton(page, 'Grid');
		await page.click('[aria-label="Clear model, size, and date filters"]');
		await page.waitForFunction(
			() => !document.querySelector('[aria-label="Clear model, size, and date filters"]'),
			{ timeout: WAIT_MS }
		);
		assert.equal(await page.evaluate(() => window.__offsetRootLifecycle.queries.length), 2);
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			offsets,
			'clearing a Grid filter keeps offsets for both the cached root and its omitted peer'
		);
	} finally {
		await harness.close();
	}
});

test('turning off Starred in Grid keeps offsets outside the cached Starred roots', async () => {
	const roots = [
		lineageGeneration(
			lineageJobId(2052),
			lineageAssetId(2052),
			'Starred offset root',
			new Date(Date.now() - 60_000).toISOString()
		),
		lineageGeneration(
			lineageJobId(2053),
			lineageAssetId(2053),
			'Unstarred offset root',
			new Date(Date.now() - 100_000).toISOString()
		)
	];
	roots[0].starred_at = roots[0].created_at;
	roots[1].model_id = 'other-history-smoke';
	const recentHistory = Array.from({ length: 49 }, (_, index) => {
		const generation = lineageGeneration(
			lineageJobId(2200 + index),
			null,
			`Recent failed history row ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		);
		generation.state = 'failed';
		return generation;
	});
	const offsets = {
		[roots[0].id]: { x: 137, y: -82 },
		[roots[1].id]: { x: -211, y: 93 }
	};
	const rootQueries = [];
	const answers = {
		'/api/v1/generations?limit=50': {
			body: (url) =>
				url.searchParams.get('state') === 'succeeded' ? roots : [...recentHistory, roots[0]]
		},
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				return url.searchParams.has('starred') ? [roots[0]] : roots;
			}
		},
		'/api/v1/generations?starred=true&limit=200': { body: [roots[0]] },
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await openImagesGrid(
		answers,
		(savedOffsets) => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			localStorage.setItem('potocolom-starred', '["13000000-0000-4000-8000-000000002052"]');
			localStorage.setItem(
				'potocolom-view-v1:local:lineage-offsets',
				JSON.stringify({ version: 1, section: 'lineage-offsets', value: savedOffsets })
			);
			window.__starredOffsetLifecycle = { queries: [], completed: 0 };
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
					window.__starredOffsetLifecycle.queries.push(url.searchParams.toString());
					return originalFetch(input, init).then((response) => {
						window.__starredOffsetLifecycle.completed += 1;
						return response;
					});
				}
				return originalFetch(input, init);
			};
		},
		[offsets]
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.click('[aria-label="Filter images"] button:nth-of-type(2)');
		await page.waitForFunction(() => window.__starredOffsetLifecycle.queries.length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__starredOffsetLifecycle.completed === 2, {
			timeout: WAIT_MS
		});
		await pause(100);
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('starred'), 'true');
		await clickButton(page, 'Grid');
		await page.click('[aria-label="Filter images"] button:nth-of-type(2)');
		await page.waitForFunction(
			() =>
				document
					.querySelector('[aria-label="Filter images"] button:nth-of-type(2)')
					?.getAttribute('aria-pressed') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.equal(await page.evaluate(() => window.__starredOffsetLifecycle.queries.length), 2);
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			offsets,
			'turning Starred off keeps offsets for the cached root and its omitted peer'
		);
	} finally {
		await harness.close();
	}
});

test('a failed unfiltered Lineage replacement keeps offsets outside its filtered roots', async () => {
	const roots = [
		lineageGeneration(
			lineageJobId(2060),
			lineageAssetId(2060),
			'Offset A',
			new Date(Date.now() - 60_000).toISOString()
		),
		lineageGeneration(
			lineageJobId(2061),
			lineageAssetId(2061),
			'Offset B',
			new Date(Date.now() - 100_000).toISOString()
		)
	];
	roots[1].model_id = 'other-history-smoke';
	const recentHistory = Array.from({ length: 49 }, (_, index) => {
		const generation = lineageGeneration(
			lineageJobId(2150 + index),
			null,
			`Recent failed history row ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		);
		generation.state = 'failed';
		return generation;
	});
	const offsets = {
		[roots[0].id]: { x: 137, y: -82 },
		[roots[1].id]: { x: -211, y: 93 }
	};
	const rootQueries = [];
	let failNextUnfiltered = false;
	const answers = {
		'/api/v1/generations?limit=50': {
			body: (url) =>
				url.searchParams.get('state') === 'succeeded' ? roots : [...recentHistory, roots[0]]
		},
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': {
			body: (url) => {
				rootQueries.push(url.searchParams.toString());
				const modelId = url.searchParams.get('model_id');
				if (modelId === null && failNextUnfiltered) {
					failNextUnfiltered = false;
					throw new Error('simulated unfiltered root replacement failure');
				}
				return modelId === null ? roots : roots.filter((root) => root.model_id === modelId);
			}
		},
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{ body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]) }
			])
		)
	};
	const harness = await openImagesGrid(
		answers,
		(savedOffsets) => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			localStorage.setItem(
				'potocolom-view-v1:local:lineage-offsets',
				JSON.stringify({ version: 1, section: 'lineage-offsets', value: savedOffsets })
			);
			window.__offsetRootLifecycle = { queries: [], completed: 0 };
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
					window.__offsetRootLifecycle.queries.push(url.searchParams.toString());
					return originalFetch(input, init).then((response) => {
						window.__offsetRootLifecycle.completed += 1;
						return response;
					});
				}
				return originalFetch(input, init);
			};
		},
		[offsets]
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(() => window.__offsetRootLifecycle.queries.length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__offsetRootLifecycle.completed === 2, {
			timeout: WAIT_MS
		});
		failNextUnfiltered = true;
		await page.click('[aria-label="Clear model, size, and date filters"]');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent.trim() === 'Retry'
				),
			{ timeout: WAIT_MS }
		);
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('model_id'), null);
		await pause(100);
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			offsets,
			'a failed replacement keeps offsets for roots omitted by the cached filtered page'
		);
	} finally {
		await harness.close();
	}
});

test('un-starring a root in Grid invalidates cached Starred roots for Lineage', async () => {
	const fixtures = imageGridFixtures(1);
	const root = fixtures.rows[0];
	let rootIsStarred = true;
	const rootQueries = [];
	const answers = fixtures.answers;
	answers['/api/v1/generations?roots_only=true&limit=50'] = {
		body: (url) => {
			rootQueries.push(url.searchParams.toString());
			return url.searchParams.has('starred') ? (rootIsStarred ? [root] : []) : [root];
		}
	};
	answers['/api/v1/generations?starred=true&limit=200'] = {
		body: () => (rootIsStarred ? [root] : [])
	};
	answers[`/api/v1/generations/${root.id}/star`] = {
		body: (_url, request) => {
			rootIsStarred = request.method !== 'DELETE';
			return {};
		}
	};
	answers[`/api/v1/generations/${root.id}/subtree`] = {
		body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)])
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
		localStorage.setItem('potocolom-starred', '["13000000-0000-4000-8000-000000000201"]');
		window.__starRootLifecycle = { queries: [], completed: 0 };
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
				window.__starRootLifecycle.queries.push(url.searchParams.toString());
				return originalFetch(input, init).then((response) => {
					window.__starRootLifecycle.completed += 1;
					return response;
				});
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${root.assets[0].id}"]`, { timeout: WAIT_MS });
		await page.click('[aria-label="Filter images"] button:nth-of-type(2)');
		await page.waitForFunction(() => window.__starRootLifecycle.queries.length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__starRootLifecycle.completed === 2, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'Grid');
		await page.waitForSelector(`[data-generation-id="${root.id}"]`, { timeout: WAIT_MS });
		await page.click(`[data-generation-id="${root.id}"]`);
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await clickButton(page, 'Unstar');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 0, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'Lineage');
		await page.waitForFunction(() => window.__starRootLifecycle.queries.length === 3, {
			timeout: 1800
		});
		assert.equal(new URLSearchParams(rootQueries.at(-1)).get('starred'), 'true');
		assert.equal(await page.$(`[data-lineage-node="${root.assets[0].id}"]`), null);
	} finally {
		await harness.close();
	}
});

test('a canceled forced Starred root reload is repeated when Lineage returns', async () => {
	const root = lineageGeneration(
		lineageJobId(2080),
		lineageAssetId(2080),
		'Canceled starred root reload',
		new Date().toISOString()
	);
	root.starred_at = root.created_at;
	let rootIsStarred = true;
	let holdNextStarredPage = false;
	let releaseHeldPage = () => {};
	const rootQueries = [];
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?roots_only=true&limit=50': {
			body: async (url) => {
				rootQueries.push(url.searchParams.toString());
				if (url.searchParams.has('starred') && holdNextStarredPage) {
					holdNextStarredPage = false;
					await new Promise((resolve) => (releaseHeldPage = resolve));
				}
				return url.searchParams.has('starred') ? (rootIsStarred ? [root] : []) : [root];
			}
		},
		'/api/v1/generations?limit=50&starred=true': {
			body: () => (rootIsStarred ? [root] : [])
		},
		'/api/v1/generations?starred=true&limit=200': {
			body: () => (rootIsStarred ? [root] : [])
		},
		[`/api/v1/generations/${root.id}/star`]: {
			body: (_url, request) => {
				rootIsStarred = request.method === 'POST';
				return {};
			}
		},
		[`/api/v1/generations/${root.id}/subtree`]: {
			body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)])
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
		localStorage.setItem('potocolom-starred', '["13000000-0000-4000-8000-000000002080"]');
		window.__starRootLifecycle = { queries: [], completed: 0, aborts: 0 };
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
				window.__starRootLifecycle.queries.push(url.searchParams.toString());
				init?.signal?.addEventListener('abort', () => {
					window.__starRootLifecycle.aborts += 1;
				});
				return originalFetch(input, init).then((response) => {
					window.__starRootLifecycle.completed += 1;
					return response;
				});
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${root.assets[0].id}"]`, { timeout: WAIT_MS });
		await page.click(`[data-lineage-node="${root.assets[0].id}"]`);
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.click('[aria-label="Filter images"] button:nth-of-type(2)');
		await page.waitForFunction(() => window.__starRootLifecycle.queries.length === 2, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__starRootLifecycle.completed === 2, {
			timeout: WAIT_MS
		});
		holdNextStarredPage = true;
		await clickButton(page, 'Unstar');
		await page.waitForFunction(() => window.__starRootLifecycle.queries.length === 3, {
			timeout: WAIT_MS
		});
		await clickButton(page, 'Grid');
		await page.waitForFunction(() => window.__starRootLifecycle.aborts > 0, {
			timeout: WAIT_MS
		});
		releaseHeldPage();
		await clickButton(page, 'Lineage');
		await page.waitForFunction(() => window.__starRootLifecycle.queries.length === 4, {
			timeout: 1800
		});
		assert.equal(
			new URLSearchParams(rootQueries.at(-1)).get('starred'),
			'true',
			'returning to Lineage repeats the canceled forced root query'
		);
		assert.equal(await page.$(`[data-lineage-node="${root.assets[0].id}"]`), null);
	} finally {
		releaseHeldPage();
		await harness.close();
	}
});

test('grid paging pins preset dates and defers grid requests until Grid mode', async () => {
	const rows = Array.from({ length: 50 }, (_, index) =>
		lineageGeneration(
			lineageJobId(280 + index),
			lineageAssetId(280 + index),
			`Paged image ${index + 1}`,
			new Date().toISOString()
		)
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50&': {
			body: (url) => (url.searchParams.has('cursor') ? [] : rows)
		},
		'/api/v1/generations?roots_only=true&limit=50': { body: [] }
	};
	const harness = await launchStudio(
		'en',
		() => {
			localStorage.setItem('potocolom-images-mode', 'lineage');
			window.__gridRequests = [];
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (
					url.pathname === '/api/v1/generations' &&
					url.searchParams.get('limit') === '50' &&
					!url.searchParams.has('roots_only') &&
					!url.searchParams.has('fields')
				) {
					window.__gridRequests.push(url.searchParams.toString());
				}
				return originalFetch(input, init);
			};
		},
		answers,
		'domcontentloaded',
		'/app?view=images'
	);
	try {
		const { page } = harness;
		await page.waitForSelector('[aria-label="Filter by creation date"]', { timeout: WAIT_MS });
		assert.equal(
			await page.evaluate(() => window.__gridRequests.length),
			1,
			'Lineage loads the shared history feed once and no grid page'
		);
		await clickButton(page, 'Grid');
		await page.waitForFunction(() => window.__gridRequests.length === 2, { timeout: WAIT_MS });
		await page.evaluate(() => (window.__gridRequests = []));
		await chooseImageOption(page, 'Filter by creation date', 'Last 7 days');
		await page.waitForFunction(
			() =>
				document.querySelectorAll('.grid-tile').length === 50 && window.__gridRequests.length > 0,
			{ timeout: WAIT_MS }
		);
		const firstPage = new URLSearchParams(await page.evaluate(() => window.__gridRequests.at(-1)));
		const firstPageBounds = {
			from: firstPage.get('created_from'),
			before: firstPage.get('created_before')
		};
		assert.ok(firstPageBounds.from && firstPageBounds.before);
		await page.evaluate(() => {
			const NativeDate = Date;
			window.Date = class extends NativeDate {
				constructor(...args) {
					super(...(args.length ? args : ['2042-04-17T12:00:00Z']));
				}
				static now() {
					return NativeDate.parse('2042-04-17T12:00:00Z');
				}
			};
		});
		await clickButton(page, 'More matching results');
		await page.waitForFunction(() => window.__gridRequests.length >= 2, { timeout: WAIT_MS });
		const nextPage = new URLSearchParams(await page.evaluate(() => window.__gridRequests.at(-1)));
		assert.equal(nextPage.get('created_from'), firstPageBounds.from);
		assert.equal(nextPage.get('created_before'), firstPageBounds.before);
		assert.equal(nextPage.get('cursor'), rows.at(-1).id);
	} finally {
		await harness.close();
	}
});

test('new matching history appears in the grid while nonmatching history stays out', async () => {
	const initial = lineageGeneration(
		lineageJobId(250),
		lineageAssetId(250),
		'Other model still running',
		new Date().toISOString()
	);
	initial.model_id = 'other-history-smoke';
	initial.state = 'running';
	const matching = lineageGeneration(
		lineageJobId(251),
		lineageAssetId(251),
		'New matching completion',
		new Date().toISOString()
	);
	const nonmatching = lineageGeneration(
		lineageJobId(252),
		lineageAssetId(252),
		'New different model completion',
		new Date().toISOString()
	);
	nonmatching.model_id = 'other-history-smoke';
	let historyReads = 0;
	const answers = {
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?limit=50': {
			body: () => {
				historyReads += 1;
				return historyReads < 3 ? [initial] : [matching, nonmatching];
			}
		},
		'/api/v1/generations?limit=50&': { body: [] },
		'/api/v1/generations?roots_only=true&limit=50': { body: [] }
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.EventSource = class {
			constructor() {
				setTimeout(() => this.onerror?.(new Event('error')), 0);
			}
			close() {}
		};
	});
	try {
		const { page } = harness;
		await chooseImageModel(page, 'History smoke model');
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('.grid-tile')].some(
					(tile) => tile.title === 'New matching completion'
				),
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.$('[title="New different model completion"]'),
			null,
			'a live row outside the model selection is not rendered'
		);
		await page.$eval(`[data-generation-id="${matching.id}"]`, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		const readsBeforeIncompleteDate = historyReads;
		await chooseImageOption(page, 'Filter by creation date', 'Custom dates');
		await page.waitForSelector('#image-filter-from');
		await page.$eval('#image-filter-from', (input) => {
			input.value = '2035-01-01';
			input.dispatchEvent(new Event('change', { bubbles: true }));
		});
		await page.waitForFunction(() => document.body.textContent.includes('Choose both dates'));
		await pause(250);
		assert.equal(historyReads, readsBeforeIncompleteDate, 'incomplete dates send no grid request');
		assert.notEqual(
			await page.$('[title="New matching completion"]'),
			null,
			'an incomplete date edit keeps a matching live row visible'
		);
		assert.notEqual(
			await page.$('.selection-inspector'),
			null,
			'an incomplete date edit keeps the selected inspector open'
		);
		const retainedPreferences = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.deepEqual(retainedPreferences.value.filters.models, [MODEL.id]);
		assert.equal(retainedPreferences.value.filters.range, 'all');
	} finally {
		await harness.close();
	}
});

test('refreshing a selected image keeps keyboard focus in the search field', async () => {
	const selected = lineageGeneration(
		lineageJobId(260),
		lineageAssetId(260),
		'Keyboard focus result',
		new Date().toISOString()
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?': {
			body: (url) => {
				if (url.searchParams.has('roots_only')) return [];
				if (url.searchParams.has('fields')) return { ids: [selected.id] };
				return [selected];
			}
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__focusGridPages = { started: [], completed: [] };
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			const isGridPage =
				url.pathname === '/api/v1/generations' &&
				url.searchParams.get('state') === 'succeeded' &&
				url.searchParams.has('limit') &&
				!url.searchParams.has('fields') &&
				!url.searchParams.has('roots_only');
			if (!isGridPage) return originalFetch(input, init);
			const query = url.searchParams.toString();
			window.__focusGridPages.started.push(query);
			return originalFetch(input, init).then((response) => {
				window.__focusGridPages.completed.push(query);
				return response;
			});
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-generation-id="${selected.id}"]`, { timeout: WAIT_MS });
		await page.$eval(`[data-generation-id="${selected.id}"]`, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.focus('[aria-label="Search prompts"]');
		await page.keyboard.type('focus');
		await page.waitForFunction(
			() =>
				window.__focusGridPages.completed.some((query) => {
					const params = new URLSearchParams(query);
					return params.get('q') === 'focus' && params.get('state') === 'succeeded';
				}),
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(
			() => document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-label')),
			'Search prompts',
			'the owned page refresh keeps focus in the search field'
		);
		await page.keyboard.type(' next');
		assert.equal(
			await page.$eval('[aria-label="Search prompts"]', (input) => input.value),
			'focus next'
		);
	} finally {
		await harness.close();
	}
});

test('active search admits completed matches only after refreshed server IDs confirm them', async () => {
	const today = new Date();
	today.setHours(12, 0, 0, 0);
	const createdAt = today.toISOString();
	const pending = lineageGeneration(lineageJobId(260), null, 'Needle pending result', createdAt);
	pending.state = 'running';
	pending.starred_at = createdAt;
	const pendingFinished = lineageGeneration(
		pending.id,
		lineageAssetId(260),
		'Needle pending result',
		createdAt
	);
	pendingFinished.starred_at = createdAt;
	const newlyMatched = lineageGeneration(
		lineageJobId(261),
		lineageAssetId(261),
		'Needle new completion',
		createdAt
	);
	newlyMatched.starred_at = createdAt;
	const wrongAction = lineageGeneration(
		lineageJobId(262),
		lineageAssetId(262),
		'Needle image edit',
		createdAt,
		lineageAssetId(899)
	);
	wrongAction.starred_at = createdAt;
	const wrongSearch = lineageGeneration(
		lineageJobId(263),
		lineageAssetId(263),
		'Unrelated prompt',
		createdAt
	);
	wrongSearch.starred_at = createdAt;
	const wrongModel = lineageGeneration(
		lineageJobId(264),
		lineageAssetId(264),
		'Needle other model',
		createdAt
	);
	wrongModel.model_id = 'other-history-smoke';
	wrongModel.starred_at = createdAt;
	const wrongDate = lineageGeneration(
		lineageJobId(265),
		lineageAssetId(265),
		'Needle old result',
		new Date(Date.UTC(2000, 0, 1, 12)).toISOString()
	);
	wrongDate.starred_at = wrongDate.created_at;
	const wrongStar = lineageGeneration(
		lineageJobId(266),
		lineageAssetId(266),
		'Needle not starred',
		createdAt
	);
	const favorites = [pending, newlyMatched, wrongAction, wrongSearch, wrongModel, wrongDate];
	let historyReads = 0;
	let searchBounds = null;
	const answers = {
		'/api/v1/generations/models': {
			body: {
				models: [MODEL, { ...MODEL, id: 'other-history-smoke', name: 'Other history model' }]
			}
		},
		'/api/v1/generations?limit=50': {
			body: () => {
				historyReads += 1;
				return historyReads < 3
					? [pending]
					: [
							pendingFinished,
							newlyMatched,
							wrongAction,
							wrongSearch,
							wrongModel,
							wrongDate,
							wrongStar
						];
			}
		},
		'/api/v1/generations?limit=50&': { body: [] },
		'/api/v1/generations?fields=ids': {
			body: (url) => {
				assert.equal(url.searchParams.get('q'), 'needle');
				assert.equal(url.searchParams.get('starred'), 'true');
				assert.equal(url.searchParams.get('model_id'), MODEL.id);
				assert.ok(url.searchParams.has('created_from'));
				assert.ok(url.searchParams.has('created_before'));
				const bounds = {
					from: url.searchParams.get('created_from'),
					before: url.searchParams.get('created_before')
				};
				if (searchBounds === null) searchBounds = bounds;
				else
					assert.deepEqual(
						bounds,
						searchBounds,
						'refreshed ID searches keep the selected date window'
					);
				return {
					ids:
						historyReads < 3
							? [pending.id, wrongAction.id]
							: [pending.id, newlyMatched.id, wrongAction.id]
				};
			}
		},
		'/api/v1/generations?starred=true&limit=200': { body: favorites },
		'/api/v1/generations?roots_only=true&limit=50': { body: [] }
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.EventSource = class {
			constructor() {
				setTimeout(() => this.onerror?.(new Event('error')), 0);
			}
			close() {}
		};
	});
	try {
		const { page } = harness;
		await clickButton(page, 'Starred');
		await page.$eval('[aria-label="Filter images"] button:nth-of-type(3)', (button) =>
			button.click()
		);
		await chooseImageModel(page, 'History smoke model');
		await chooseImageOption(page, 'Filter by creation date', 'Today');
		await page.evaluate(() => {
			const NativeDate = Date;
			window.Date = class extends NativeDate {
				constructor(...args) {
					super(...(args.length ? args : ['2042-04-17T12:00:00Z']));
				}
				static now() {
					return NativeDate.parse('2042-04-17T12:00:00Z');
				}
			};
		});
		await page.type('input[name="q"]', 'needle');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 0, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('input[name="q"]')].some(
					(input) => input.value === 'needle'
				) && document.body.innerText.includes('Grid'),
			{ timeout: WAIT_MS }
		);
		await page.waitForFunction(
			() =>
				[...document.querySelectorAll('.grid-tile')].some(
					(tile) => tile.title === 'Needle pending result'
				) &&
				[...document.querySelectorAll('.grid-tile')].some(
					(tile) => tile.title === 'Needle new completion'
				),
			{ timeout: WAIT_MS }
		);
		const visibleTitles = await page.$$eval('.grid-tile', (tiles) =>
			tiles.map((tile) => tile.title)
		);
		assert.deepEqual(visibleTitles.sort(), ['Needle new completion', 'Needle pending result']);
	} finally {
		await harness.close();
	}
});

test('Grid restores a bounded cursor window and offers Newest without filling a history gap', async () => {
	const rows = Array.from({ length: 320 }, (_, index) =>
		lineageGeneration(
			lineageJobId(700 + index),
			lineageAssetId(700 + index),
			`Cursor resume image ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const anchor = rows[120];
	const predecessor = rows[119];
	const selected = rows[130];
	const preferences = {
		version: 1,
		section: 'images',
		value: {
			mode: 'grid',
			filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
			action: 'all',
			search: '',
			starredOnly: false,
			gridAnchor: { id: anchor.id, beforeCursor: predecessor.id, offset: 24 },
			gridSelectionId: selected.id,
			lineageSelection: null
		}
	};
	const requests = [];
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=200': {
			body: (url) => {
				requests.push(url.searchParams.toString());
				assert.equal(url.searchParams.get('cursor'), predecessor.id);
				assert.equal(url.searchParams.get('state'), 'succeeded');
				return rows.slice(120, 320);
			}
		},
		'/api/v1/generations?limit=50': {
			body: (url) => {
				requests.push(url.searchParams.toString());
				return rows.slice(0, 50);
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': { body: [] }
	};
	const harness = await openImagesGrid(
		answers,
		(seededPreferences) => {
			localStorage.setItem('potocolom-view-v1:local:images', JSON.stringify(seededPreferences));
		},
		[preferences]
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-generation-id="${anchor.id}"]`, { timeout: WAIT_MS });
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.notEqual(await page.$(`[data-generation-id="${selected.id}"]`), null);
		assert.equal(
			await page.$eval(
				'.selection-inspector',
				(element, prompt) => element.textContent?.includes(prompt),
				selected.params.prompt
			),
			true
		);
		assert.ok(
			requests.some((query) => new URLSearchParams(query).get('cursor') === predecessor.id)
		);
		assert.equal(
			await page.evaluate(() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === 'Newest'
				)
			),
			true
		);
		assert.equal(
			await page.$eval('button[aria-label="Newest"]', (button) => {
				const rect = button.getBoundingClientRect();
				return (
					button.textContent?.trim() === 'Newest' && rect.top >= 0 && rect.bottom <= innerHeight
				);
			}),
			true,
			'the accessible Newest control stays visible above the result window'
		);
		const tileCountBeforeNewest = await page.$$eval('.grid-tile', (tiles) => tiles.length);
		assert.equal(tileCountBeforeNewest, 200, 'restore reads at most one bounded page');
		await page.$eval('button[aria-label="Newest"]', (button) => button.click());
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		assert.equal(new URLSearchParams(requests.at(-1)).has('cursor'), false);
		assert.equal(
			await page.evaluate(() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === 'Newest'
				)
			),
			false
		);
	} finally {
		await harness.close();
	}
});

test('Grid drops a deleted saved cursor and fetches newest once', async () => {
	const rows = Array.from({ length: 12 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1100 + index),
			lineageAssetId(1100 + index),
			`Deleted cursor fallback ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const staleCursor = lineageJobId(1099);
	const staleAnchor = lineageJobId(1098);
	const preferences = {
		version: 1,
		section: 'images',
		value: {
			mode: 'grid',
			filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
			action: 'all',
			search: '',
			starredOnly: false,
			gridAnchor: { id: staleAnchor, beforeCursor: staleCursor, offset: 0 },
			gridSelectionId: null,
			lineageSelection: null
		}
	};
	const requests = [];
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=200': {
			status: 404,
			body: (url) => {
				requests.push(url.searchParams.toString());
				return null;
			}
		},
		'/api/v1/generations?limit=50': {
			body: (url) => {
				requests.push(url.searchParams.toString());
				return rows;
			}
		},
		'/api/v1/generations?limit=50&': {
			body: (url) => {
				requests.push(url.searchParams.toString());
				return rows;
			}
		},
		'/api/v1/generations?roots_only=true&limit=50': { body: [] }
	};
	const harness = await openImagesGrid(
		answers,
		(seededPreferences) => {
			localStorage.setItem('potocolom-view-v1:local:images', JSON.stringify(seededPreferences));
		},
		[preferences]
	);
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 12, {
			timeout: WAIT_MS
		});
		assert.equal(
			requests.filter((query) => {
				const params = new URLSearchParams(query);
				return params.get('limit') === '50' && params.get('state') === 'succeeded';
			}).length,
			1,
			'a stale cursor causes exactly one newest fallback request'
		);
		assert.equal(new URLSearchParams(requests.at(-1)).has('cursor'), false);
		assert.equal(
			await page.evaluate(() =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === 'Newest'
				)
			),
			false
		);
	} finally {
		await harness.close();
	}
});

test('Grid saves applied filters, search, action, scroll, and selection across navigation and reload', async () => {
	const rows = Array.from({ length: 60 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1300 + index),
			lineageAssetId(1300 + index),
			`Retained prompt ${String(index).padStart(2, '0')}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?': {
			body: (url) => {
				if (url.searchParams.has('roots_only')) return [];
				if (url.searchParams.has('fields')) return { ids: rows.map((row) => row.id) };
				const cursor = url.searchParams.get('cursor');
				const start = cursor === null ? 0 : rows.findIndex((row) => row.id === cursor) + 1;
				assert.ok(start >= 0, 'the requested cursor belongs to this matching row set');
				const limit = Number(url.searchParams.get('limit') ?? '50');
				return rows.slice(start, start + limit);
			}
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__gridPageReads = { started: [], completed: [] };
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (
				url.pathname === '/api/v1/generations' &&
				url.searchParams.get('state') === 'succeeded' &&
				url.searchParams.has('limit') &&
				!url.searchParams.has('fields') &&
				!url.searchParams.has('roots_only')
			) {
				const query = url.searchParams.toString();
				window.__gridPageReads.started.push(query);
				return originalFetch(input, init).then((response) => {
					window.__gridPageReads.completed.push(query);
					return response;
				});
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await clickButton(page, 'Grid');
		await chooseImageModel(page, 'History smoke model');
		await page.evaluate(() => {
			const action = [...document.querySelectorAll('.filter-pill')].find(
				(button) => button.textContent?.trim() === 'Generated'
			);
			if (!action) throw new Error('Grid Generated action filter was not found');
			action.click();
		});
		await chooseImageOption(page, 'Filter by creation date', 'Last 7 days');
		await page.$eval('[aria-label="Search prompts"]', (input) => {
			input.value = 'retained';
			input.dispatchEvent(new Event('input', { bubbles: true }));
		});
		await page.waitForFunction(
			() =>
				window.__gridPageReads.completed.some((query) => {
					const params = new URLSearchParams(query);
					return params.get('q') === 'retained' && params.get('action') === 'generate';
				}),
			{ timeout: WAIT_MS }
		);
		await page.waitForSelector(`[data-generation-id="${rows[25].id}"]`, { timeout: WAIT_MS });
		await page.waitForFunction(
			() => document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		await page.$eval(`[data-generation-id="${rows[25].id}"]`, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		const selectionPrefs = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.equal(
			selectionPrefs.value.gridSelectionId,
			rows[25].id,
			`selection was not persisted after click: ${JSON.stringify(selectionPrefs.value)}`
		);
		await page.$eval(`[data-generation-id="${rows[32].id}"]`, (tile) =>
			tile.scrollIntoView({ block: 'center' })
		);
		await pause(50);
		const afterScroll = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.equal(
			afterScroll.value.gridSelectionId,
			rows[25].id,
			`scroll cleared selection before anchor save: ${JSON.stringify(afterScroll.value)}`
		);
		await pause(400);
		const saved = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.equal(saved.value.mode, 'grid');
		assert.deepEqual(saved.value.filters.models, [MODEL.id]);
		assert.equal(saved.value.filters.range, 'last7');
		assert.equal(saved.value.action, 'generate');
		assert.equal(saved.value.search, 'retained');
		assert.equal(saved.value.gridSelectionId, rows[25].id, JSON.stringify(saved.value));
		assert.ok(saved.value.gridAnchor);

		await clickStudioView(page, 'metrics');
		await page.waitForFunction(() => document.body.innerText.includes('GPU activity'), {
			timeout: WAIT_MS
		});
		await clickStudioView(page, 'images', '.grid-scroller');
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.equal(
			await page.$eval('[aria-label="Search prompts"]', (input) => input.value),
			'retained'
		);
		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.equal(
			await page.$eval('[aria-label="Search prompts"]', (input) => input.value),
			'retained'
		);
		assert.notEqual(await page.$(`[data-generation-id="${rows[25].id}"]`), null);
	} finally {
		await harness.close();
	}
});

test('Grid warm return keeps the newest window and brings new images to the top', async () => {
	const rows = Array.from({ length: 100 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1400 + index),
			lineageAssetId(1400 + index),
			`Warm newest image ${String(index).padStart(2, '0')}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const added = lineageGeneration(
		lineageJobId(1550),
		lineageAssetId(1550),
		'Newly finished image',
		new Date(Date.now() + 1000).toISOString()
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?': {
			body: (url) => {
				if (url.searchParams.has('roots_only')) return [];
				const cursor = url.searchParams.get('cursor');
				const start = cursor === null ? 0 : rows.findIndex((row) => row.id === cursor) + 1;
				assert.ok(start >= 0, 'the cursor belongs to the newest fixture history');
				return rows.slice(start, start + Number(url.searchParams.get('limit') ?? '50'));
			}
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__warmNewestQueries = [];
		const nativeStartViewTransition = document.startViewTransition?.bind(document);
		window.__warmNewestTransitions = {
			supported: Boolean(nativeStartViewTransition),
			started: 0,
			active: 0
		};
		if (nativeStartViewTransition) {
			document.startViewTransition = (update) => {
				const transition = nativeStartViewTransition(update);
				window.__warmNewestTransitions.started += 1;
				window.__warmNewestTransitions.active += 1;
				transition.finished.then(
					() => (window.__warmNewestTransitions.active -= 1),
					() => (window.__warmNewestTransitions.active -= 1)
				);
				return transition;
			};
		}
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.get('state') === 'succeeded') {
				window.__warmNewestQueries.push(url.searchParams.toString());
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		await page.$eval(`[data-generation-id="${rows[30].id}"]`, (tile) =>
			tile.scrollIntoView({ block: 'center' })
		);
		await page.waitForFunction(
			() => {
				const envelope = JSON.parse(
					localStorage.getItem('potocolom-view-v1:local:images') ?? 'null'
				);
				return envelope?.value?.gridAnchor?.id !== undefined;
			},
			{ timeout: WAIT_MS }
		);
		const saved = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.ok(saved.value.gridAnchor?.beforeCursor);

		const beforeGenerateTransition = await page.evaluate(
			() => window.__warmNewestTransitions.started
		);
		await clickButton(page, 'Generate');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === null, {
			timeout: WAIT_MS
		});
		await page.waitForSelector('#gen-prompt', { timeout: WAIT_MS });
		await page.waitForFunction(
			(before) =>
				window.__warmNewestTransitions.started > before &&
				window.__warmNewestTransitions.active === 0,
			{ timeout: WAIT_MS },
			beforeGenerateTransition
		);
		rows.unshift(added);
		const queryStart = await page.evaluate(() => window.__warmNewestQueries.length);
		const beforeImagesTransition = await page.evaluate(
			() => window.__warmNewestTransitions.started
		);
		await clickButton(page, 'Images');
		await page.waitForFunction(
			(before) =>
				window.__warmNewestTransitions.started > before &&
				window.__warmNewestTransitions.active === 0,
			{ timeout: WAIT_MS },
			beforeImagesTransition
		);
		await page
			.waitForFunction(
				() =>
					document.querySelectorAll('.grid-tile').length >= 50 &&
					document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
				{ timeout: WAIT_MS }
			)
			.catch(async (error) => {
				const state = await page.evaluate(() => ({
					url: location.href,
					rows: document.querySelectorAll('.grid-tile').length,
					busy: document.querySelector('.grid-scroller')?.getAttribute('aria-busy'),
					mode: [...document.querySelectorAll('[role="group"] button')].map((button) => ({
						text: button.textContent?.trim(),
						pressed: button.getAttribute('aria-pressed')
					})),
					requests: window.__warmNewestQueries,
					prefs: localStorage.getItem('potocolom-view-v1:local:images')
				}));
				throw new Error(`${error.message}; warm newest state ${JSON.stringify(state)}`);
			});
		const newRequests = await page.evaluate(
			(start) => window.__warmNewestQueries.slice(start),
			queryStart
		);
		const resumed = newRequests
			.map((query) => new URLSearchParams(query))
			.find((params) => params.get('limit') === '200');
		assert.ok(resumed, `expected a bounded warm refresh: ${JSON.stringify(newRequests)}`);
		assert.equal(resumed.get('cursor'), null, 'a warm newest window refresh has no cursor');
		assert.equal(await page.$('button[aria-label="Newest"]'), null);
		await page.waitForSelector(`[data-generation-id="${added.id}"]`, { timeout: WAIT_MS });
		const rowIds = await page.$$eval('.grid-tile', (tiles) =>
			tiles.map((tile) => tile.getAttribute('data-generation-id'))
		);
		assert.equal(rowIds[0], added.id, 'the new completed image appears at the newest edge');
		assert.ok(
			rows.slice(1, 31).every((row) => rowIds.includes(row.id)),
			'previously loaded images above the saved anchor remain reachable in the warm window'
		);
		await page.waitForFunction(
			({ id, offset }) => {
				const scroller = document.querySelector('.grid-scroller');
				const tile = scroller?.querySelector(`[data-generation-id="${CSS.escape(id)}"]`);
				return Boolean(
					scroller &&
					tile &&
					Math.abs(
						tile.getBoundingClientRect().top - scroller.getBoundingClientRect().top - offset
					) < 2
				);
			},
			{ timeout: WAIT_MS },
			saved.value.gridAnchor
		);
	} finally {
		await harness.close();
	}
});

test('Grid warm newest refresh makes one bounded cursor request when the anchor moves past row 200', async () => {
	const rows = Array.from({ length: 450 }, (_, index) =>
		lineageGeneration(
			lineageJobId(3000 + index),
			lineageAssetId(3000 + index),
			`Bounded warm image ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?': {
			body: (url) => {
				if (url.searchParams.has('roots_only')) return [];
				const cursor = url.searchParams.get('cursor');
				const start = cursor === null ? 0 : rows.findIndex((row) => row.id === cursor) + 1;
				assert.ok(start >= 0, 'the cursor belongs to the matching fixture history');
				return rows.slice(start, start + Number(url.searchParams.get('limit') ?? '50'));
			}
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__boundedWarmQueries = [];
		const nativeStartViewTransition = document.startViewTransition?.bind(document);
		window.__boundedWarmTransitions = {
			supported: Boolean(nativeStartViewTransition),
			started: 0,
			active: 0
		};
		if (nativeStartViewTransition) {
			document.startViewTransition = (update) => {
				const transition = nativeStartViewTransition(update);
				window.__boundedWarmTransitions.started += 1;
				window.__boundedWarmTransitions.active += 1;
				transition.finished.then(
					() => (window.__boundedWarmTransitions.active -= 1),
					() => (window.__boundedWarmTransitions.active -= 1)
				);
				return transition;
			};
		}
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations' && url.searchParams.get('state') === 'succeeded')
				window.__boundedWarmQueries.push(url.searchParams.toString());
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		for (const expectedCount of [50, 100, 150, 200]) {
			await page.waitForFunction(
				(count) => document.querySelectorAll('.grid-tile').length === count,
				{ timeout: WAIT_MS },
				expectedCount
			);
			if (expectedCount < 200) await clickButton(page, 'More matching results');
		}
		const anchorCandidate = rows[180];
		await page.$eval(`[data-generation-id="${anchorCandidate.id}"]`, (tile) =>
			tile.scrollIntoView({ block: 'center' })
		);
		await page.waitForFunction(
			() => {
				const envelope = JSON.parse(
					localStorage.getItem('potocolom-view-v1:local:images') ?? 'null'
				);
				return Boolean(envelope?.value?.gridAnchor?.beforeCursor);
			},
			{ timeout: WAIT_MS }
		);
		const saved = await readViewPrefs(page, { kind: 'local' }, 'images');
		const originalAnchorIndex = rows.findIndex((row) => row.id === saved.value.gridAnchor.id);
		assert.ok(
			originalAnchorIndex >= 100 && originalAnchorIndex < 200,
			`anchor index ${originalAnchorIndex}`
		);

		const beforeGenerateTransition = await page.evaluate(
			() => window.__boundedWarmTransitions.started
		);
		await clickButton(page, 'Generate');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === null, {
			timeout: WAIT_MS
		});
		await page.waitForSelector('#gen-prompt', { timeout: WAIT_MS });
		await page.waitForFunction(
			(before) =>
				window.__boundedWarmTransitions.started > before &&
				window.__boundedWarmTransitions.active === 0,
			{ timeout: WAIT_MS },
			beforeGenerateTransition
		);
		const inserted = Array.from({ length: 100 }, (_, index) =>
			lineageGeneration(
				lineageJobId(3600 + index),
				lineageAssetId(3600 + index),
				`New warm completion ${index}`,
				new Date(Date.now() + (100 - index) * 1000).toISOString()
			)
		);
		rows.unshift(...inserted);
		const queryStart = await page.evaluate(() => window.__boundedWarmQueries.length);
		const beforeImagesTransition = await page.evaluate(
			() => window.__boundedWarmTransitions.started
		);
		await clickButton(page, 'Images');
		await page.waitForFunction(
			(before) =>
				window.__boundedWarmTransitions.started > before &&
				window.__boundedWarmTransitions.active === 0,
			{ timeout: WAIT_MS },
			beforeImagesTransition
		);
		await page.waitForFunction(
			(start) => window.__boundedWarmQueries.length >= start + 1,
			{ timeout: WAIT_MS },
			queryStart
		);
		const firstRequest = await page.evaluate(
			(start) => window.__boundedWarmQueries[start],
			queryStart
		);
		const firstParams = new URLSearchParams(firstRequest);
		assert.equal(firstParams.get('limit'), '200');
		assert.equal(
			firstParams.get('cursor'),
			null,
			`the first refresh asks for the newest bounded page: ${firstRequest}`
		);
		await page.waitForFunction(
			(start) => window.__boundedWarmQueries.length >= start + 2,
			{ timeout: WAIT_MS },
			queryStart
		);
		await page.waitForFunction(
			() => document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		const requests = await page.evaluate(
			(start) => window.__boundedWarmQueries.slice(start),
			queryStart
		);
		const params = requests.map((query) => new URLSearchParams(query));
		assert.equal(
			params.length,
			2,
			`one newest page plus one cursor recovery: ${JSON.stringify(requests)}`
		);
		assert.equal(params[1].get('limit'), '200');
		assert.equal(params[1].get('cursor'), saved.value.gridAnchor.beforeCursor);
		assert.notEqual(await page.$(`[data-generation-id="${saved.value.gridAnchor.id}"]`), null);
		assert.equal(
			(await page.$('button[aria-label="Newest"]')) === null,
			false,
			'the restored cursor window keeps the Newest action'
		);
	} finally {
		await harness.close();
	}
});

test('Grid selection survives leaving Images while its cold resume is pending', async () => {
	const rows = Array.from({ length: 260 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1600 + index),
			lineageAssetId(1600 + index),
			`Pending selection image ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	let announceResume;
	let releaseResume;
	const resumeStarted = new Promise((resolve) => (announceResume = resolve));
	const heldResume = new Promise((resolve) => (releaseResume = resolve));
	const preferences = {
		version: 1,
		section: 'images',
		value: {
			mode: 'grid',
			filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
			action: 'all',
			search: '',
			starredOnly: false,
			gridAnchor: { id: rows[1].id, beforeCursor: rows[0].id, offset: 24 },
			gridSelectionId: rows[25].id,
			lineageSelection: null
		}
	};
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=200': {
			body: async (url) => {
				if (url.searchParams.has('cursor')) {
					announceResume();
					await heldResume;
				}
				const cursor = url.searchParams.get('cursor');
				const start = cursor === null ? 0 : rows.findIndex((row) => row.id === cursor) + 1;
				return rows.slice(start, start + Number(url.searchParams.get('limit') ?? '50'));
			}
		},
		'/api/v1/generations?limit=50': { body: rows.slice(0, 50) }
	};
	const harness = await launchStudio(
		'en',
		(seededPreferences) => {
			localStorage.setItem('potocolom-view-v1:local:images', JSON.stringify(seededPreferences));
		},
		answers,
		'domcontentloaded',
		'/app?view=images',
		[preferences]
	);
	try {
		const { page } = harness;
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await waitFixture(resumeStarted, 'the saved selection resume did not begin');
		await clickButton(page, 'Metrics');
		await page.waitForFunction(
			() =>
				new URL(location.href).searchParams.get('view') === 'metrics' &&
				document.body.innerText.includes('GPU activity') &&
				!document.querySelector('[aria-label="Filter by model"]') &&
				!document.querySelector('.grid-scroller'),
			{ timeout: WAIT_MS }
		);
		const saved = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.equal(saved.value.gridSelectionId, rows[25].id);
	} finally {
		releaseResume();
		await harness.close();
	}
});

test('a cold restored selection does not steal focus from a Grid control', async () => {
	const rows = Array.from({ length: 80 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1700 + index),
			lineageAssetId(1700 + index),
			`Cold focus image ${index}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	let announceResume;
	let releaseResume;
	const resumeStarted = new Promise((resolve) => (announceResume = resolve));
	const heldResume = new Promise((resolve) => (releaseResume = resolve));
	const preferences = {
		version: 1,
		section: 'images',
		value: {
			mode: 'grid',
			filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
			action: 'all',
			search: '',
			starredOnly: false,
			gridAnchor: { id: rows[1].id, beforeCursor: rows[0].id, offset: 18 },
			gridSelectionId: rows[25].id,
			lineageSelection: null
		}
	};
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=200': {
			body: async (url) => {
				if (url.searchParams.has('cursor')) {
					announceResume();
					await heldResume;
				}
				return rows.slice(1, 1 + Number(url.searchParams.get('limit') ?? '50'));
			}
		},
		'/api/v1/generations?limit=50': { body: rows.slice(0, 50) }
	};
	const harness = await launchStudio(
		'en',
		(seededPreferences) => {
			localStorage.setItem('potocolom-view-v1:local:images', JSON.stringify(seededPreferences));
		},
		answers,
		'domcontentloaded',
		'/app?view=images',
		[preferences]
	);
	try {
		const { page } = harness;
		await page.waitForSelector('[aria-label="Filter by model"]', { timeout: WAIT_MS });
		await waitFixture(resumeStarted, 'the cold selection restore did not start');
		await page.focus('[aria-label="Search prompts"]');
		releaseResume();
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.waitForFunction(
			() => document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-label')),
			'Search prompts',
			'a cold restored inspector leaves the focused Grid control active'
		);
	} finally {
		releaseResume();
		await harness.close();
	}
});

test('Grid reload selection closes on one Back before leaving Images', async () => {
	const fixtures = imageGridFixtures(1);
	const harness = await openGenerateThenImages(fixtures.answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
	});
	try {
		const { page } = harness;
		const tile = `[data-generation-id="${fixtures.rows[0].id}"]`;
		await page.waitForSelector(tile, { timeout: WAIT_MS });
		await page.$eval(tile, (element) => element.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		await page.evaluate(() => history.back());
		await page.waitForFunction(
			() =>
				!document.querySelector('.selection-inspector') &&
				new URL(location.href).searchParams.get('view') === 'images',
			{ timeout: WAIT_MS }
		);
		await page.evaluate(() => history.back());
		await page.waitForFunction(
			(previousUrl) => location.href === previousUrl,
			{
				timeout: WAIT_MS
			},
			harness.previousUrl
		);
	} finally {
		await harness.close();
	}
});

test('Lineage reload selection closes on one Back, including a repeated reload', async () => {
	const root = lineageGeneration(
		lineageJobId(1750),
		lineageAssetId(1750),
		'Lineage Back root',
		new Date().toISOString(),
		null,
		{ hasDerivatives: true }
	);
	const child = lineageGeneration(
		lineageJobId(1751),
		lineageAssetId(1751),
		'Lineage Back child',
		new Date(Date.now() - 1000).toISOString(),
		root.assets[0].id
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': { body: [root, child] },
		'/api/v1/generations?roots_only=true&limit=50': { body: [root] },
		[`/api/v1/generations/${root.id}/subtree`]: {
			body: lineageSubtree([
				lineageSubtreeNode(root, root.assets[0].id, null),
				lineageSubtreeNode(child, child.assets[0].id, root.id)
			])
		}
	};
	const harness = await openGenerateThenImages(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
	});
	try {
		const { page } = harness;
		const node = `[data-lineage-node="${child.assets[0].id}"]`;
		await page.waitForSelector(node, { timeout: WAIT_MS });
		await page.$eval(node, (element) => element.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		for (let attempt = 0; attempt < 2; attempt += 1) {
			await page.reload({ waitUntil: 'domcontentloaded' });
			await page.waitForSelector(node, { timeout: WAIT_MS });
			await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
			await page.evaluate(() => history.back());
			await page.waitForFunction(
				() =>
					!document.querySelector('.selection-inspector') &&
					new URL(location.href).searchParams.get('view') === 'images',
				{ timeout: WAIT_MS }
			);
			if (attempt === 0) {
				await page.$eval(node, (element) => element.click());
				await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
			}
		}
		await page.evaluate(() => history.back());
		await page.waitForFunction(
			(previousUrl) => location.href === previousUrl,
			{
				timeout: WAIT_MS
			},
			harness.previousUrl
		);
	} finally {
		await harness.close();
	}
});

test('Grid user scrolling cancels a delayed saved-position restore after a warm return', async () => {
	const rows = Array.from({ length: 100 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1500 + index),
			lineageAssetId(1500 + index),
			`Warm resume image ${String(index).padStart(2, '0')}`,
			new Date(Date.now() - index * 1000).toISOString()
		)
	);
	const pageRows = (url) => {
		const cursor = url.searchParams.get('cursor');
		const start = cursor === null ? 0 : rows.findIndex((row) => row.id === cursor) + 1;
		assert.ok(start >= 0, 'resume cursor belongs to the successful fixture history');
		return rows.slice(start, start + Number(url.searchParams.get('limit') ?? '50'));
	};
	let announceResumeRequest;
	let releaseResumeResponse;
	const resumeRequestStarted = new Promise((resolve) => (announceResumeRequest = resolve));
	const heldResumeResponse = new Promise((resolve) => (releaseResumeResponse = resolve));
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=200': {
			body: async (url) => {
				announceResumeRequest();
				await heldResumeResponse;
				return pageRows(url);
			}
		},
		'/api/v1/generations?limit=50&': { body: pageRows },
		'/api/v1/generations?limit=50': { body: rows.slice(0, 50) },
		'/api/v1/generations?roots_only=true&limit=50': { body: [] }
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'grid');
		window.__warmGridResume = { started: 0, completed: 0, requests: [] };
		const nativeStartViewTransition = document.startViewTransition?.bind(document);
		window.__warmGridTransitions = {
			supported: Boolean(nativeStartViewTransition),
			started: 0,
			active: 0
		};
		if (nativeStartViewTransition) {
			document.startViewTransition = (update) => {
				const transition = nativeStartViewTransition(update);
				window.__warmGridTransitions.started += 1;
				window.__warmGridTransitions.active += 1;
				transition.finished.then(
					() => (window.__warmGridTransitions.active -= 1),
					() => (window.__warmGridTransitions.active -= 1)
				);
				return transition;
			};
		}
		const originalFetch = window.fetch.bind(window);
		window.fetch = (input, init) => {
			const url = new URL(typeof input === 'string' ? input : input.url, location.href);
			if (url.pathname === '/api/v1/generations') {
				window.__warmGridResume.requests.push(url.searchParams.toString());
			}
			if (
				url.pathname === '/api/v1/generations' &&
				url.searchParams.get('limit') === '200' &&
				url.searchParams.get('state') === 'succeeded'
			) {
				window.__warmGridResume.started += 1;
				return originalFetch(input, init).then((response) => {
					window.__warmGridResume.completed += 1;
					return response;
				});
			}
			return originalFetch(input, init);
		};
	});
	try {
		const { page } = harness;
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		await page.$eval(
			'.grid-scroller',
			(scroller, anchorId) => {
				scroller.style.overflowAnchor = 'none';
				document.querySelector(`[data-generation-id="${anchorId}"]`).scrollIntoView({
					block: 'center'
				});
			},
			rows[35].id
		);
		await page.waitForFunction(
			async () => {
				const value = JSON.parse(localStorage.getItem('potocolom-view-v1:local:images') ?? 'null');
				return value?.value?.gridAnchor?.id !== undefined;
			},
			{ timeout: WAIT_MS }
		);
		await pause(300);
		const savedPrefs = await readViewPrefs(page, { kind: 'local' }, 'images');
		assert.ok(savedPrefs.value.gridAnchor);
		assert.ok(savedPrefs.value.gridAnchor.beforeCursor);

		await clickButton(page, 'Metrics');
		await page.waitForFunction(
			() => new URL(location.href).searchParams.get('view') === 'metrics',
			{ timeout: WAIT_MS }
		);
		const transitionsBeforeReturn = await page.evaluate(() => window.__warmGridTransitions.started);
		await clickButton(page, 'Images');
		await page.waitForFunction(() => document.querySelectorAll('.grid-tile').length === 50, {
			timeout: WAIT_MS
		});
		await waitFixture(resumeRequestStarted, 'saved cursor request did not start on return');
		await page.waitForFunction(
			(before) =>
				window.__warmGridTransitions.supported &&
				window.__warmGridTransitions.started > before &&
				window.__warmGridTransitions.active === 0,
			{ timeout: WAIT_MS },
			transitionsBeforeReturn
		);
		await page.waitForFunction(
			({ id, offset }) => {
				const scroller = document.querySelector('.grid-scroller');
				const tile = scroller?.querySelector(`[data-generation-id="${CSS.escape(id)}"]`);
				if (!scroller || !tile) return false;
				return (
					Math.abs(
						tile.getBoundingClientRect().top - scroller.getBoundingClientRect().top - offset
					) < 2
				);
			},
			{ timeout: WAIT_MS },
			savedPrefs.value.gridAnchor
		);
		await page
			.waitForFunction(() => window.__warmGridResume.started === 1, { timeout: WAIT_MS })
			.catch(async (error) => {
				const state = await page.evaluate(() => ({
					url: location.href,
					prefs: localStorage.getItem('potocolom-view-v1:local:images'),
					requests: window.__warmGridResume?.requests,
					started: window.__warmGridResume?.started,
					completed: window.__warmGridResume?.completed,
					busy: document.querySelector('.grid-scroller')?.getAttribute('aria-busy'),
					rows: [...document.querySelectorAll('.grid-tile')].map((tile) =>
						tile.getAttribute('data-generation-id')
					)
				}));
				throw new Error(`${error.message}; warm resume state: ${JSON.stringify(state)}`);
			});
		const resumeQuery = await page.evaluate(() =>
			window.__warmGridResume.requests.find((query) => {
				const params = new URLSearchParams(query);
				return params.get('limit') === '200' && params.get('state') === 'succeeded';
			})
		);
		assert.ok(resumeQuery, 'the warm response uses the bounded successful-history request');
		assert.equal(
			new URLSearchParams(resumeQuery).get('cursor'),
			null,
			'a newest-first warm snapshot refresh does not request a middle window'
		);
		const scroller = await page.$('.grid-scroller');
		const bounds = await scroller.boundingBox();
		assert.ok(bounds);
		const beforeWheel = await page.$eval('.grid-scroller', (element) => {
			element.addEventListener('wheel', () => (window.__sawGridWheel = true), { once: true });
			return element.scrollTop;
		});
		const wheelDelta = await page.$eval('.grid-scroller', (element) => {
			const maxScrollTop = element.scrollHeight - element.clientHeight;
			return element.scrollTop >= maxScrollTop - 1 ? -160 : 160;
		});
		await page.mouse.move(bounds.x + 40, bounds.y + 40);
		await page.mouse.wheel({ deltaY: wheelDelta });
		await page.waitForFunction(
			({ before, deltaY }) => {
				const scroller = document.querySelector('.grid-scroller');
				if (!scroller || window.__sawGridWheel !== true) return false;
				return deltaY < 0 ? scroller.scrollTop < before : scroller.scrollTop > before;
			},
			{ timeout: WAIT_MS },
			{ before: beforeWheel, deltaY: wheelDelta }
		);
		const userScrollTop = await page.$eval('.grid-scroller', (element) => element.scrollTop);
		const warmRowsBeforeResponse = await page.$$eval('.grid-tile', (tiles) =>
			tiles.map((tile) => tile.getAttribute('data-generation-id'))
		);
		const viewportBeforeRefresh = await page.$eval('.grid-scroller', (element) => ({
			visible: [...element.querySelectorAll('.grid-tile')]
				.filter((tile) => {
					const rect = tile.getBoundingClientRect();
					const scroller = element.getBoundingClientRect();
					return rect.bottom > scroller.top && rect.top < scroller.bottom;
				})
				.map((tile) => tile.getAttribute('data-generation-id')),
			height: element.scrollHeight,
			client: element.clientHeight
		}));
		assert.ok(
			wheelDelta < 0 ? userScrollTop < beforeWheel : userScrollTop > beforeWheel,
			`wheel input moves warm grid before refresh completes: ${beforeWheel} to ${userScrollTop} (delta ${wheelDelta})`
		);
		releaseResumeResponse();
		await page.waitForFunction(
			() =>
				window.__warmGridResume.completed === 1 &&
				document.querySelector('.grid-scroller')?.getAttribute('aria-busy') === 'false',
			{ timeout: WAIT_MS }
		);
		await pause(100);
		const afterRefresh = await page.$eval('.grid-scroller', (element) => ({
			top: element.scrollTop,
			height: element.scrollHeight,
			client: element.clientHeight,
			rows: [...element.querySelectorAll('.grid-tile')].map((tile) =>
				tile.getAttribute('data-generation-id')
			),
			visible: [...element.querySelectorAll('.grid-tile')]
				.filter((tile) => {
					const rect = tile.getBoundingClientRect();
					const scroller = element.getBoundingClientRect();
					return rect.bottom > scroller.top && rect.top < scroller.bottom;
				})
				.map((tile) => tile.getAttribute('data-generation-id'))
		}));
		assert.ok(
			Math.abs(afterRefresh.top - userScrollTop) < 2,
			`refresh kept the user scroll position: ${userScrollTop} to ${afterRefresh.top}; before=${JSON.stringify(viewportBeforeRefresh)} after=${JSON.stringify(afterRefresh)}`
		);
		assert.deepEqual(
			afterRefresh.rows,
			warmRowsBeforeResponse,
			'a canceled warm resume keeps its already committed row window'
		);
	} finally {
		releaseResumeResponse();
		await harness.close();
	}
});

test('Lineage keeps a selected child and its viewport across navigation and reload', async () => {
	const createdAt = new Date().toISOString();
	const root = lineageGeneration(
		lineageJobId(1800),
		lineageAssetId(1800),
		'Restored lineage root',
		createdAt,
		null,
		{ hasDerivatives: true }
	);
	const child = lineageGeneration(
		lineageJobId(1801),
		lineageAssetId(1801),
		'Restored lineage child',
		new Date(Date.now() - 1000).toISOString(),
		root.assets[0].id
	);
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': { body: [root, child] },
		'/api/v1/generations?roots_only=true&limit=50': { body: [root] },
		[`/api/v1/generations/${root.id}/subtree`]: {
			body: lineageSubtree([
				lineageSubtreeNode(root, root.assets[0].id, null),
				lineageSubtreeNode(child, child.assets[0].id, root.id)
			])
		}
	};
	const harness = await openImagesGrid(answers, () => {
		localStorage.setItem('potocolom-images-mode', 'lineage');
	});
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${child.assets[0].id}"]`, { timeout: WAIT_MS });
		await page.$eval(`[data-lineage-node="${child.assets[0].id}"]`, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.match(
			await page.$eval('.selection-inspector', (inspector) => inspector.innerText),
			/Restored lineage child/
		);
		await pause(350);
		const savedImages = await readViewPrefs(page, { kind: 'local' }, 'images');
		const savedViewport = await readViewPrefs(page, { kind: 'local' }, 'lineage-viewport');
		assert.deepEqual(savedImages.value.lineageSelection, {
			rootId: root.id,
			assetId: child.assets[0].id
		});
		assert.ok(savedViewport, 'the viewport has a saved owner-scoped value');

		await clickStudioView(page, 'metrics');
		await clickStudioView(page, 'images', '.lineage-viewport');
		await page.waitForSelector(`[data-lineage-node="${child.assets[0].id}"]`, { timeout: WAIT_MS });
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.match(
			await page.$eval('.selection-inspector', (inspector) => inspector.innerText),
			/Restored lineage child/
		);
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'images')).value.lineageSelection,
			savedImages.value.lineageSelection
		);
		assert.deepEqual(
			await readViewPrefs(page, { kind: 'local' }, 'lineage-viewport'),
			savedViewport
		);

		await page.reload({ waitUntil: 'domcontentloaded' });
		await page.waitForSelector(`[data-lineage-node="${child.assets[0].id}"]`, { timeout: WAIT_MS });
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.match(
			await page.$eval('.selection-inspector', (inspector) => inspector.innerText),
			/Restored lineage child/
		);
		assert.equal(
			await page.$eval(`[data-lineage-node="${child.assets[0].id}"]`, (tile) =>
				tile.classList.contains('is-selected')
			),
			true
		);
		assert.deepEqual(
			await readViewPrefs(page, { kind: 'local' }, 'lineage-viewport'),
			savedViewport
		);
	} finally {
		await harness.close();
	}
});

test('Lineage drops a missing child from a complete loaded subtree while more roots remain', async () => {
	const roots = Array.from({ length: 50 }, (_, index) =>
		lineageGeneration(
			lineageJobId(1900 + index),
			lineageAssetId(1900 + index),
			`Missing selection root ${index}`,
			new Date(Date.now() - index * 1000).toISOString(),
			null,
			{ hasDerivatives: index === 0 }
		)
	);
	const removedAssetId = lineageAssetId(1999);
	const unrelatedOffsets = { [roots[49].id]: { x: 42, y: -31 } };
	const preferences = {
		version: 1,
		section: 'images',
		value: {
			mode: 'lineage',
			filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
			action: 'all',
			search: '',
			starredOnly: false,
			gridAnchor: null,
			gridSelectionId: null,
			lineageSelection: { rootId: roots[0].id, assetId: removedAssetId }
		}
	};
	const offsetsPreferences = {
		version: 1,
		section: 'lineage-offsets',
		value: unrelatedOffsets
	};
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': { body: roots },
		'/api/v1/generations?roots_only=true&limit=50': { body: roots },
		...Object.fromEntries(
			roots.map((root) => [
				`/api/v1/generations/${root.id}/subtree`,
				{
					body: lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)])
				}
			])
		)
	};
	const harness = await openImagesGrid(
		answers,
		(imagesPrefs, offsetsPrefs) => {
			localStorage.setItem('potocolom-view-v1:local:images', JSON.stringify(imagesPrefs));
			localStorage.setItem('potocolom-view-v1:local:lineage-offsets', JSON.stringify(offsetsPrefs));
			window.__missingSelectionRoots = 0;
			const originalFetch = window.fetch.bind(window);
			window.fetch = (input, init) => {
				const url = new URL(typeof input === 'string' ? input : input.url, location.href);
				if (url.pathname === '/api/v1/generations' && url.searchParams.has('roots_only')) {
					return originalFetch(input, init).then(async (response) => {
						window.__missingSelectionRoots = (await response.clone().json()).length;
						return response;
					});
				}
				return originalFetch(input, init);
			};
		},
		[preferences, offsetsPreferences]
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${roots[0].assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(() => window.__missingSelectionRoots === 50, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(
			async () => {
				const value = JSON.parse(localStorage.getItem('potocolom-view-v1:local:images') ?? 'null');
				return value?.value?.lineageSelection === null;
			},
			{ timeout: WAIT_MS }
		);
		assert.equal(await page.$('.selection-inspector'), null);
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'lineage-offsets')).value,
			unrelatedOffsets,
			'dropping a missing selection keeps offsets for unrelated roots'
		);
	} finally {
		await harness.close();
	}
});

test('Lineage retains a missing child hint when its loaded subtree is truncated', async () => {
	const root = lineageGeneration(
		lineageJobId(2000),
		lineageAssetId(2000),
		'Truncated selection root',
		new Date().toISOString(),
		null,
		{ hasDerivatives: true }
	);
	const missingSelection = {
		version: 1,
		section: 'images',
		value: {
			mode: 'lineage',
			filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
			action: 'all',
			search: '',
			starredOnly: false,
			gridAnchor: null,
			gridSelectionId: null,
			lineageSelection: { rootId: root.id, assetId: lineageAssetId(2099) }
		}
	};
	const truncatedSubtree = {
		...lineageSubtree([lineageSubtreeNode(root, root.assets[0].id, null)]),
		truncated: true,
		remaining_count_lower_bound: 1
	};
	const answers = {
		'/api/v1/generations/models': { body: { models: [MODEL] } },
		'/api/v1/generations?limit=50': { body: [root] },
		'/api/v1/generations?roots_only=true&limit=50': { body: [root] },
		[`/api/v1/generations/${root.id}/subtree`]: { body: truncatedSubtree }
	};
	const harness = await openImagesGrid(
		answers,
		(preferences) => {
			localStorage.setItem('potocolom-view-v1:local:images', JSON.stringify(preferences));
		},
		[missingSelection]
	);
	try {
		const { page } = harness;
		await page.waitForSelector(`[data-lineage-node="${root.assets[0].id}"]`, {
			timeout: WAIT_MS
		});
		await page.waitForFunction(
			() =>
				document
					.querySelector('.lineage-world')
					?.textContent?.includes('At least 1 more not loaded'),
			{ timeout: WAIT_MS }
		);
		assert.deepEqual(
			(await readViewPrefs(page, { kind: 'local' }, 'images')).value.lineageSelection,
			missingSelection.value.lineageSelection,
			'a truncated subtree cannot prove that the saved child was deleted'
		);
		assert.equal(await page.$('.selection-inspector'), null);
	} finally {
		await harness.close();
	}
});

async function waitForStrip(page, thumbs) {
	await page.waitForFunction(
		(expected) => document.querySelectorAll('button[data-strip-thumb]').length === expected,
		{ timeout: WAIT_MS },
		thumbs
	);
}

async function stripState(page) {
	return page.evaluate(() => {
		const thumbs = [...document.querySelectorAll('button[data-strip-thumb]')];
		const stops = thumbs.filter((thumb) => thumb.getAttribute('tabindex') === '0');
		return {
			stops: stops.length,
			stop: thumbs.indexOf(stops[0]),
			focus: thumbs.indexOf(document.activeElement),
			focusTabIndex: document.activeElement?.getAttribute('tabindex') ?? null,
			current: thumbs.findIndex((thumb) => thumb.getAttribute('aria-current') === 'true')
		};
	});
}

async function clickStripThumb(page, index) {
	const thumbs = await page.$$('button[data-strip-thumb]');
	assert.equal(thumbs.length, 6, 'six thumbnails before clicking');
	assert.ok(thumbs[index], `thumbnail ${index} must exist`);
	await thumbs[index].click();
}

async function assertStripStop(page, expected, label) {
	const state = await stripState(page);
	assert.equal(state.stops, 1, `${label}: exactly one thumbnail carries tabindex 0`);
	assert.equal(state.stop, expected, `${label}: the tab stop is at index ${expected}`);
	assert.equal(state.focus, expected, `${label}: focus is at index ${expected}`);
	assert.equal(state.focusTabIndex, '0', `${label}: the focused thumbnail carries the stop`);
}

test('expired lineage roots stay visible beside their descendants and the root grid', async () => {
	const fixtures = lineageCanvasFixtures();
	const harness = await launchStudio(
		'en',
		() => {
			localStorage.setItem(
				'potocolom-lineage-viewport',
				JSON.stringify({
					translateX: 300,
					translateY: 0,
					scale: 0.3,
					rootId: null,
					anchorX: null,
					anchorY: null
				})
			);
			window.__lineageImageRequests = [];
			new PerformanceObserver((list) => {
				for (const entry of list.getEntries()) {
					if (entry.initiatorType === 'img' && entry.name.includes('/api/v1/assets/'))
						window.__lineageImageRequests.push(entry.name);
				}
			}).observe({ type: 'resource', buffered: true });
		},
		fixtures.answers,
		'domcontentloaded',
		'/app?view=images'
	);
	try {
		const { page } = harness;
		const fallbackRootSelector = `[data-lineage-node="${LINEAGE_ROOT_JOB}"]`;
		const expiredRootSelector = `[data-lineage-node="${fixtures.expiredAssetId}"]`;
		const expectedAssets = [
			fixtures.expiredAssetId,
			...fixtures.chainAssetIds,
			...fixtures.fanoutAssetIds,
			...fixtures.looseAssetIds
		];
		await page.waitForSelector(fallbackRootSelector, { timeout: WAIT_MS });
		await page.$eval(fallbackRootSelector, (tile) => tile.click());
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.match(
			await page.$eval('.selection-inspector', (inspector) => inspector.innerText),
			/Image unavailable/
		);
		assert.match(
			await page.$eval('.selection-inspector', (inspector) => inspector.innerText),
			/GPU time\s+42 ms/
		);
		await page.waitForFunction(
			(ids) => ids.every((id) => document.querySelector(`[data-lineage-node="${id}"]`)),
			{ timeout: WAIT_MS },
			expectedAssets
		);

		const ghost = await page.$eval(expiredRootSelector, (tile) => ({
			missing: tile.classList.contains('is-missing'),
			label: tile.getAttribute('title'),
			images: tile.querySelectorAll('img').length
		}));
		assert.deepEqual(ghost, {
			missing: true,
			label: 'Expired ancestor prompt',
			images: 0
		});
		assert.deepEqual(await page.evaluate(() => window.__lineageImageRequests), []);
		await page.waitForSelector('.selection-inspector', { timeout: WAIT_MS });
		assert.equal(
			await page.$eval(expiredRootSelector, (tile) => tile.classList.contains('is-selected')),
			true,
			'selection follows the ghost onto its missing subtree node'
		);
		assert.match(
			await page.$eval('.selection-inspector', (inspector) => inspector.innerText),
			/Image unavailable/
		);
		await clickButton(page, 'Close selected image');

		const gridPositions = await page.evaluate((ids) => {
			return ids.map((id) => {
				const tile = document.querySelector(`[data-lineage-node="${id}"]`);
				const transform = tile?.closest('.tile-shell')?.style.transform ?? '';
				const match = transform.match(/translate3d\((-?[\d.]+)px, (-?[\d.]+)px/);
				if (!match) throw new Error(`grid tile ${id} has no layout position`);
				return { id, x: Number(match[1]), y: Number(match[2]) };
			});
		}, fixtures.looseAssetIds);
		assert.equal(gridPositions.length, 6);
		assert.equal(new Set(gridPositions.map((position) => position.x)).size, 1);
		assert.deepEqual(
			gridPositions.map((position) => position.y),
			gridPositions.map((_, index) => index * 224),
			'the six loose roots occupy consecutive grid rows'
		);

		const before = await page.evaluate(() => {
			const viewport = document.querySelector('.lineage-viewport');
			const world = document.querySelector('.lineage-world');
			const rect = viewport.getBoundingClientRect();
			const x = Math.round(rect.left + rect.width * 0.45);
			const y = Math.round(rect.top + rect.height * 0.55);
			const transform = new DOMMatrixReadOnly(getComputedStyle(world).transform);
			const point = transform.inverse().transformPoint(new DOMPoint(x - rect.left, y - rect.top));
			const positions = Object.fromEntries(
				[...document.querySelectorAll('.lineage-tile[data-lineage-node]')].map((tile) => [
					tile.dataset.lineageNode,
					tile.closest('.tile-shell').style.transform
				])
			);
			return {
				lod: [...world.classList].find((name) => name.startsWith('lod-')),
				point: [point.x, point.y],
				positions,
				x,
				y
			};
		});
		await page.mouse.move(before.x, before.y);
		await page.mouse.wheel({ deltaY: 250 });
		await page.waitForFunction(
			(previous) =>
				document.querySelector('.lineage-world')?.classList.contains('lod-constellation') &&
				!document.querySelector('.lineage-world')?.classList.contains(previous),
			{ timeout: WAIT_MS },
			before.lod
		);
		const after = await page.evaluate(
			(cursor) => {
				const viewport = document.querySelector('.lineage-viewport');
				const world = document.querySelector('.lineage-world');
				const rect = viewport.getBoundingClientRect();
				const transform = new DOMMatrixReadOnly(getComputedStyle(world).transform);
				const point = transform
					.inverse()
					.transformPoint(new DOMPoint(cursor.x - rect.left, cursor.y - rect.top));
				const positions = Object.fromEntries(
					[...document.querySelectorAll('.lineage-tile[data-lineage-node]')].map((tile) => [
						tile.dataset.lineageNode,
						tile.closest('.tile-shell').style.transform
					])
				);
				return {
					point: [point.x, point.y],
					positions
				};
			},
			{ x: before.x, y: before.y }
		);
		assert.ok(Math.abs(before.point[0] - after.point[0]) < 0.01);
		assert.ok(Math.abs(before.point[1] - after.point[1]) < 0.01);
		assert.deepEqual(after.positions, before.positions);
	} finally {
		await harness.close();
	}
});

test('the history strip gives six thumbnails exactly one tab stop', async () => {
	const harness = await openStudio(stripFixtures());
	try {
		const { page } = harness;
		await waitForStrip(page, 6);
		const state = await stripState(page);
		assert.equal(state.stops, 1, 'exactly one thumbnail carries tabindex 0');
		assert.equal(state.stop, state.current, 'the stop sits on the thumbnail marked aria-current');
	} finally {
		await harness.close();
	}
});

test('arrow, Home and End keys move the strip tab stop together with focus', async () => {
	const harness = await openStudio(stripFixtures());
	try {
		const { page } = harness;
		await waitForStrip(page, 6);
		await clickStripThumb(page, 2);
		await assertStripStop(page, 2, 'clicking the third thumbnail');
		for (const expected of [3, 4, 5]) {
			await page.keyboard.press('ArrowRight');
			await assertStripStop(page, expected, `ArrowRight to index ${expected}`);
		}
		await page.keyboard.press('Home');
		await assertStripStop(page, 0, 'Home');
		await page.keyboard.press('End');
		await assertStripStop(page, 5, 'End');
		await page.keyboard.press('ArrowLeft');
		await assertStripStop(page, 4, 'ArrowLeft');
	} finally {
		await harness.close();
	}
});

test('the strip tab stop stays on the shown thumbnail when starred items arrive in front', async () => {
	const harness = await openStudio(stripFixtures({ body: STRIP_STARRED, delayMs: 1200 }));
	try {
		const { page } = harness;
		await waitForStrip(page, 6);
		await clickStripThumb(page, 2);
		await assertStripStop(page, 2, 'before the starred items arrive');
		await waitForStrip(page, 8);
		const state = await stripState(page);
		assert.equal(state.stops, 1, 'exactly one thumbnail carries tabindex 0');
		assert.equal(state.stop, state.current, 'the tab stop is the thumbnail marked aria-current');
		assert.equal(state.stop, 4, 'the shown thumbnail moved behind the two starred ones');
		assert.notEqual(state.stop, 0, 'the first starred thumbnail must not take the tab stop');
	} finally {
		await harness.close();
	}
});

test('the history strip has an aria-label', async () => {
	const harness = await openStudio(stripFixtures());
	try {
		const { page } = harness;
		await waitForStrip(page, 6);
		const label = await page.$eval(
			'button[data-strip-thumb]',
			(thumb) => thumb.closest('[role="group"]')?.getAttribute('aria-label') ?? ''
		);
		assert.notEqual(label.trim(), '', 'the strip group must be named');
	} finally {
		await harness.close();
	}
});

test('a pen stroke with varying pressure paints a thick segment and a thin segment', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await setBrushSize(page, 20);
		await pointerStroke(page, 'pen', 1, [
			{ x: 0.1, y: 0.5, pressure: 1.0 },
			{ x: 0.3, y: 0.5, pressure: 1.0 },
			{ x: 0.5, y: 0.5, pressure: 0.1 },
			{ x: 0.7, y: 0.5, pressure: 0.1 },
			{ x: 0.9, y: 0.5, pressure: 0.1 }
		]);
		assert.deepEqual(
			await surfacePixel(page, 200, 249),
			[17, 24, 39, 255],
			'a full pressure segment paints at full width, reaching 7px off center'
		);
		assert.deepEqual(
			await surfacePixel(page, 400, 249),
			[255, 255, 255, 255],
			'a low pressure segment narrows, leaving 7px off center as paper'
		);
	} finally {
		await harness.close();
	}
});

test('a mouse stroke paints at a fixed width regardless of pointer pressure', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await setBrushSize(page, 20);
		await pointerStroke(page, 'mouse', 1, [
			{ x: 0.1, y: 0.5, pressure: 0.1 },
			{ x: 0.3, y: 0.5, pressure: 0.1 },
			{ x: 0.5, y: 0.5, pressure: 1.0 },
			{ x: 0.7, y: 0.5, pressure: 1.0 },
			{ x: 0.9, y: 0.5, pressure: 1.0 }
		]);
		assert.deepEqual(
			await surfacePixel(page, 200, 249),
			[17, 24, 39, 255],
			'mouse pressure must not narrow the stroke'
		);
		assert.deepEqual(
			await surfacePixel(page, 400, 249),
			[17, 24, 39, 255],
			'mouse pressure must not widen the stroke either, width stays fixed'
		);
	} finally {
		await harness.close();
	}
});

test('a pen without a pressure sensor paints at the full brush size', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await setBrushSize(page, 20);
		await pointerStroke(page, 'pen', 1, [
			{ x: 0.1, y: 0.5, pressure: 0.5 },
			{ x: 0.3, y: 0.5, pressure: 0.5 },
			{ x: 0.5, y: 0.5, pressure: 0.5 }
		]);
		assert.deepEqual(
			await surfacePixel(page, 200, 249),
			[17, 24, 39, 255],
			'a constant 0.5 is an unsensed pen, so the stroke keeps the full width'
		);
	} finally {
		await harness.close();
	}
});

test('a touch pointer is ignored while a pen stroke is active, and the pen stroke still completes', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		await setBrushSize(page, 20);
		await dispatchPointer(page, 'pointerdown', {
			pointerId: 1,
			pointerType: 'pen',
			pressure: 0.8,
			x: 0.2,
			y: 0.5
		});
		await dispatchPointer(page, 'pointermove', {
			pointerId: 1,
			pointerType: 'pen',
			pressure: 0.8,
			x: 0.3,
			y: 0.5
		});
		// A palm (or a second finger) lands on the glass while the pen is still
		// drawing: it must start nothing, not even a lasso.
		await dispatchPointer(page, 'pointerdown', {
			pointerId: 2,
			pointerType: 'touch',
			x: 0.8,
			y: 0.8
		});
		await dispatchPointer(page, 'pointermove', {
			pointerId: 2,
			pointerType: 'touch',
			x: 0.85,
			y: 0.85
		});
		await dispatchPointer(page, 'pointerup', {
			pointerId: 2,
			pointerType: 'touch',
			x: 0.85,
			y: 0.85
		});
		assert.deepEqual(
			await surfacePixel(page, 410, 410),
			[255, 255, 255, 255],
			'the touch pointer must not have drawn anything'
		);
		await dispatchPointer(page, 'pointermove', {
			pointerId: 1,
			pointerType: 'pen',
			pressure: 0.8,
			x: 0.4,
			y: 0.5
		});
		await dispatchPointer(page, 'pointerup', {
			pointerId: 1,
			pointerType: 'pen',
			pressure: 0.8,
			x: 0.4,
			y: 0.5
		});
		assert.deepEqual(
			await surfacePixel(page, 180, 256),
			[17, 24, 39, 255],
			'the pen stroke itself must still have painted'
		);
		assert.equal((await button(page, 'Undo')).disabled, false);
		await clickButton(page, 'Undo');
		const blank = await bitmap(page);
		assert.equal(blank.hash !== undefined, true);
		assert.equal(
			(await button(page, 'Undo')).disabled,
			true,
			'the touch pointer added no extra undo step'
		);
	} finally {
		await harness.close();
	}
});

test('a shape drawn with a pen saves without pressure on its points', async () => {
	const downloadDirectory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-pen-shape-'));
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const client = await page.createCDPSession();
		await client.send('Browser.setDownloadBehavior', {
			behavior: 'allow',
			downloadPath: downloadDirectory
		});
		await selectTool(page, 'rectangle');
		await pointerStroke(page, 'pen', 1, [
			{ x: 0.2, y: 0.2, pressure: 0.4 },
			{ x: 0.8, y: 0.8, pressure: 0.9 }
		]);
		await clickButton(page, 'Save drawing');
		const saved = JSON.parse(
			await readFile(await waitForDrawingDownload(downloadDirectory), 'utf8')
		);
		assert.equal(saved.operations[0].kind, 'shape');
		for (const point of saved.operations[0].points) assert.equal('pressure' in point, false);
	} finally {
		await harness.close();
		await rm(downloadDirectory, { recursive: true, force: true });
	}
});

test('a version 3 drawing file without pressure still opens', async () => {
	const directory = await mkdtemp(join(tmpdir(), 'potocolom-canvas-v3-still-opens-'));
	const path = join(directory, 'v3-no-pressure.potocolom.json');
	await writeFile(
		path,
		JSON.stringify({
			version: 3,
			width: 512,
			height: 512,
			operations: [
				{
					kind: 'stroke',
					id: 'operation-1',
					mode: 'draw',
					color: '#dc2626',
					size: 12,
					points: [{ x: 128, y: 128 }]
				}
			],
			cursor: 1
		})
	);
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		await openDrawingFile(page, path);
		await page.waitForFunction(() => {
			const pixel = document
				.querySelector('canvas[aria-label="Drawing surface"]')
				.getContext('2d')
				.getImageData(128, 128, 1, 1).data;
			return pixel[0] === 220 && pixel[1] === 38 && pixel[2] === 38 && pixel[3] === 255;
		});
		await clickButton(page, 'Undo');
		await waitForBitmap(page, blank.hash);
	} finally {
		await harness.close();
		await rm(directory, { recursive: true, force: true });
	}
});
