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
const WAIT_MS = 5000;
// The frame header this build writes on the canvas socket: one kind byte, the
// 16 byte session id and a 4 byte big endian revision, then the image.
const FRAME_HEADER_BYTES = 21;
const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));
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
				const answer = apiAnswers[key] ??
					Object.entries(apiAnswers).find(
						([queryKey]) => queryKey.includes('?') && key.startsWith(queryKey)
					)?.[1] ??
					apiAnswers[path] ?? { body: defaultApiBody(path) };
				if (answer.delayMs) await pause(answer.delayMs);
				response.writeHead(200, { 'Content-Type': 'application/json' });
				response.end(JSON.stringify(answer.body));
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
async function launchStudio(locale, setup, apiAnswers, waitUntil, appPath = '/app') {
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
		page.on('pageerror', (error) => errors.push(error.message));
		await page.setViewport({ width: 1440, height: 1100 });
		await page.evaluateOnNewDocument((selectedLocale) => {
			try {
				localStorage.setItem('locale', selectedLocale);
			} catch {
				// The initial blank document can have an opaque origin.
			}
		}, locale);
		await installSocketAdapter(page);
		if (setup) await page.evaluateOnNewDocument(setup);
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

async function openCanvas(locale = 'en', setup) {
	const harness = await launchStudio(locale, setup, {}, 'networkidle0');
	try {
		const { page } = harness;
		const tab = locale === 'es' ? 'Lienzo en tiempo real' : 'Realtime canvas';
		await page.waitForFunction(
			(name) =>
				[...document.querySelectorAll('button')].some(
					(button) => button.textContent?.trim() === name
				),
			{ timeout: WAIT_MS },
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

test('a rectangle drag has one undo step and no stale preview edges', async () => {
	const harness = await openCanvas();
	try {
		const { page } = harness;
		const blank = await bitmap(page);
		assert.deepEqual(await page.select('#realtime-tool', 'rectangle'), ['rectangle']);
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
			assert.deepEqual(await page.select('#realtime-tool', shape.tool), [shape.tool]);
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
		await page.select('#realtime-tool', 'rectangle');
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
		await page.select('#realtime-tool', 'rectangle');
		await selectColor(page, 'Blue');
		await stroke(page, [0.2, 0.2], [0.8, 0.8], 8);
		const shaped = await bitmap(page);
		await page.select('#realtime-tool', 'erase');
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
			await page.select('#realtime-tool', 'ellipse');
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
		await page.select('#realtime-tool', 'ellipse');
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
		await page.select('#realtime-tool', 'ellipse');
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
			await page.select('#realtime-tool', 'draw');
			await tap(page, 0.1 + offset, 0.1 + offset);
			snapshots.push(await bitmap(page));
			await page.select('#realtime-tool', 'line');
			await stroke(page, [0.15 + offset, 0.2], [0.3 + offset, 0.2], 3);
			snapshots.push(await bitmap(page));
			await page.select('#realtime-tool', 'rectangle');
			await stroke(page, [0.2 + offset, 0.3], [0.32 + offset, 0.42], 3);
			snapshots.push(await bitmap(page));
			await page.select('#realtime-tool', 'ellipse');
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
			await page.select('#realtime-tool', 'rectangle');
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
		await page.select('#realtime-tool', 'rectangle');
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
		await page.select('#realtime-tool', 'rectangle');
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

		await page.reload({ waitUntil: 'networkidle0' });
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
		await page.select('#realtime-tool', 'erase');
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
		await page.select('#realtime-tool', 'erase');
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
		await page.select('#realtime-tool', 'draw');
		await selectColor(page, 'Black');
		await setBrushSize(page, 6);
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.5, rect.y + rect.height * 0.5);
		await page.mouse.down();
		await selectColor(page, 'Red');
		await setBrushSize(page, 32);
		await page.select('#realtime-tool', 'erase');
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
		await page.select('#realtime-tool', 'erase');
		await pause(700);
		const afterControls = await page.evaluate(() => ({
			frames: window.__historySockets[0]?.frames.length ?? 0,
			sent: window.__historySockets[0]?.sent.length ?? 0
		}));
		assert.deepEqual(afterControls, before);
		await page.select('#realtime-tool', 'draw');
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
			await page.$$eval('#realtime-tool option', (options) =>
				options.map((option) => [option.value, option.textContent.trim()])
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
		await page.select('#realtime-tool', 'erase');
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
		await page.mouse.down();
		assert.deepEqual(await bitmap(page), blank);
		assert.equal((await button(page, 'Clear canvas')).disabled, true);
		await page.mouse.up();
		await page.select('#realtime-tool', 'draw');
		await tap(page);
		await page.select('#realtime-tool', 'erase');
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
		await page.select('#realtime-tool', 'erase');
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
				await page.select('#realtime-tool', 'erase');
			} else await page.select('#realtime-tool', 'rectangle');
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
			(thumb) => thumb.closest('[role="list"]')?.getAttribute('aria-label') ?? ''
		);
		assert.notEqual(label.trim(), '', 'the strip list must be named');
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
		await page.select('#realtime-tool', 'rectangle');
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
