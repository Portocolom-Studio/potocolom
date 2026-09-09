import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { access, readFile, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import puppeteer from 'puppeteer-core';

const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
const WAIT_MS = 5000;
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
			structure_strength: { type: 'number', minimum: 0, maximum: 1, default: 0.7 }
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

async function serveBuild() {
	const server = createServer(async (request, response) => {
		try {
			const path = new URL(request.url, 'http://localhost').pathname;
			if (path.startsWith('/api/')) {
				const body =
					path === '/api/v1/models'
						? [MODEL]
						: path === '/api/v1/config'
							? { auth_methods: [], billing_enabled: false, languages: ['en', 'es'] }
							: [];
				response.writeHead(200, { 'Content-Type': 'application/json' });
				response.end(JSON.stringify(body));
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

async function openCanvas(locale = 'en', setup) {
	const server = await serveBuild();
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
		await page.goto(`http://127.0.0.1:${server.address().port}/app`, { waitUntil: 'networkidle0' });
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
		return {
			page,
			browser,
			server,
			async close() {
				await browser.close();
				await new Promise((resolveServer) => server.close(resolveServer));
				assert.deepEqual(errors, [], 'no uncaught browser errors');
			}
		};
	} catch (error) {
		if (browser) await browser.close();
		await new Promise((resolveServer) => server.close(resolveServer));
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
		assert.deepEqual(frame.slice(17, 21), [82, 73, 70, 70], 'payload is a WebP RIFF image');
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

test('eraser movement does not read the full bitmap on every pointer sample', async () => {
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
		await tap(page, 0.8, 0.8);
		await page.select('#realtime-tool', 'erase');
		const rect = await canvasRect(page);
		await page.mouse.move(rect.x + rect.width * 0.2, rect.y + rect.height * 0.2);
		await page.mouse.down();
		await page.evaluate(() => {
			window.__fullReadbacks = 0;
		});
		await page.mouse.move(rect.x + rect.width * 0.4, rect.y + rect.height * 0.4, { steps: 24 });
		assert.equal(await page.evaluate(() => window.__fullReadbacks), 0);
		await page.mouse.up();
		assert.ok(await page.evaluate(() => window.__fullReadbacks > 0));
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
