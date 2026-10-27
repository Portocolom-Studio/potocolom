#!/usr/bin/env node
import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import { access, readFile, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

const build = resolve(fileURLToPath(new URL('../build', import.meta.url)));
const manualUrl = process.env.FOOTER_TEST_URL;
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

function contrastRatio(foreground, background) {
	const luminance = (color) => {
		const channels = color
			.match(/[\d.]+/g)
			?.slice(0, 3)
			.map(Number);
		assert.ok(channels?.length === 3, `expected an RGB color, got ${color}`);
		const [r, g, b] = channels.map((value) => {
			const channel = value / 255;
			return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
		});
		return 0.2126 * r + 0.7152 * g + 0.0722 * b;
	};
	const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
	return (values[0] + 0.05) / (values[1] + 0.05);
}

async function sampleCopyrightBackground(page, reader) {
	await page.$eval('footer.horizon', (footer) => footer.scrollIntoView({ block: 'end' }));
	const line = await page.$eval('footer.horizon .band .bottom > span', (span) => {
		const box = span.getBoundingClientRect();
		const foreground = getComputedStyle(span).color;
		span.style.color = 'transparent';
		return {
			x: box.left + window.scrollX,
			y: box.top + window.scrollY,
			width: box.width,
			height: box.height,
			foreground
		};
	});
	const shot = await page.screenshot({
		clip: { x: line.x, y: line.y, width: line.width, height: line.height },
		encoding: 'base64'
	});
	await page.$eval('footer.horizon .band .bottom > span', (span) => {
		span.style.color = '';
	});
	const background = await reader.evaluate(async (data) => {
		const image = new Image();
		image.src = `data:image/png;base64,${data}`;
		await image.decode();
		const canvas = document.createElement('canvas');
		canvas.width = image.width;
		canvas.height = image.height;
		const context = canvas.getContext('2d');
		context.drawImage(image, 0, 0);
		const pixels = context.getImageData(0, 0, image.width, image.height).data;
		const samples = [];
		for (let i = 0; i < pixels.length; i += 4) {
			const sum = pixels[i] * 0.2126 + pixels[i + 1] * 0.7152 + pixels[i + 2] * 0.0722;
			samples.push([sum, pixels[i], pixels[i + 1], pixels[i + 2]]);
		}
		samples.sort((a, b) => a[0] - b[0]);
		const [, r, g, b] = samples[Math.floor(samples.length * 0.95)];
		return `rgb(${r}, ${g}, ${b})`;
	}, shot);
	return { foreground: line.foreground, background };
}

let server;
let browser;
try {
	let baseUrl = manualUrl;
	if (!baseUrl) {
		server = await serveBuild();
		baseUrl = `http://127.0.0.1:${server.address().port}`;
	}

	browser = await puppeteer.launch({
		executablePath: await chromeExecutable(),
		headless: true,
		args: ['--no-sandbox', '--disable-gpu']
	});
	// The band is a particle sea drawn on a canvas over a CSS glow and fade.
	// Check the live sea first: screenshot the copyright line with its text
	// hidden, and compare the text colour with the lightest background behind
	// it, ignoring the brightest 5% of pixels (single passing particles).
	const livePage = await browser.newPage();
	// The site's CSP blocks data: images, so screenshots are read on a blank page.
	const reader = await browser.newPage();
	let liveEngineRun;
	await livePage.evaluateOnNewDocument(() => {
		const originalGetContext = HTMLCanvasElement.prototype.getContext;
		const canvasIds = new WeakMap();
		let nextCanvasId = 0;
		window.__particleSeaContextCalls = [];
		HTMLCanvasElement.prototype.getContext = function (contextType, ...args) {
			const sea = this.closest('.particle-sea');
			let call;
			if (sea?.dataset.variant === 'footer') {
				let canvasId = canvasIds.get(this);
				if (canvasId === undefined) {
					canvasId = ++nextCanvasId;
					canvasIds.set(this, canvasId);
				}
				call = { canvasId, contextType };
				window.__particleSeaContextCalls.push(call);
			}
			const context = originalGetContext.call(this, contextType, ...args);
			if (call && contextType === 'webgl2' && context) {
				const extension = context.getExtension('WEBGL_debug_renderer_info');
				call.renderer = extension ? context.getParameter(extension.UNMASKED_RENDERER_WEBGL) : null;
			}
			return context;
		};
	});
	await livePage.goto(baseUrl, { waitUntil: 'networkidle0' });
	for (const [width, height, label] of [
		[1280, 900, 'desktop'],
		[390, 844, 'phone']
	]) {
		await livePage.setViewport({ width, height });
		await livePage.waitForSelector('footer.horizon .band .bottom');
		await livePage.$eval('footer.horizon', (footer) => footer.scrollIntoView({ block: 'end' }));
		await livePage.waitForSelector('footer.horizon .particle-sea canvas[data-live]');
		await new Promise((done) => setTimeout(done, 600));
		if (label === 'desktop') {
			const engineRun = await livePage.evaluate(() => ({
				engine: document.querySelector('footer.horizon .particle-sea')?.dataset.engine ?? null,
				contextCalls: window.__particleSeaContextCalls
			}));
			console.log(`Footer particle engine: ${JSON.stringify(engineRun)}`);
			liveEngineRun = engineRun;
		}
		const line = await livePage.$eval('footer.horizon .band .bottom > span', (span) => {
			const box = span.getBoundingClientRect();
			const foreground = getComputedStyle(span).color;
			span.style.color = 'transparent';
			// Screenshot clips are measured from the top of the document.
			return {
				x: box.left + window.scrollX,
				y: box.top + window.scrollY,
				width: box.width,
				height: box.height,
				foreground
			};
		});
		const shot = await livePage.screenshot({
			clip: { x: line.x, y: line.y, width: line.width, height: line.height },
			encoding: 'base64'
		});
		await livePage.$eval('footer.horizon .band .bottom > span', (span) => {
			span.style.color = '';
		});
		const background = await reader.evaluate(async (data) => {
			const image = new Image();
			image.src = `data:image/png;base64,${data}`;
			await image.decode();
			const canvas = document.createElement('canvas');
			canvas.width = image.width;
			canvas.height = image.height;
			const context = canvas.getContext('2d');
			context.drawImage(image, 0, 0);
			const pixels = context.getImageData(0, 0, image.width, image.height).data;
			const samples = [];
			for (let i = 0; i < pixels.length; i += 4) {
				const sum = pixels[i] * 0.2126 + pixels[i + 1] * 0.7152 + pixels[i + 2] * 0.0722;
				samples.push([sum, pixels[i], pixels[i + 1], pixels[i + 2]]);
			}
			samples.sort((a, b) => a[0] - b[0]);
			const [, r, g, b] = samples[Math.floor(samples.length * 0.95)];
			return `rgb(${r}, ${g}, ${b})`;
		}, shot);
		assert.ok(
			contrastRatio(line.foreground, background) >= 4.5,
			`${label} copyright must contrast with the live particle sea (fg ${line.foreground}, bg ${background})`
		);
	}
	await livePage.close();

	// Then block the canvas and check the CSS fallback behind it.
	const page = await browser.newPage();
	await page.evaluateOnNewDocument(() => {
		const getContext = HTMLCanvasElement.prototype.getContext;
		HTMLCanvasElement.prototype.getContext = function (...args) {
			if (this.closest('.particle-sea')) return null;
			return getContext.apply(this, args);
		};
	});

	await page.setViewport({ width: 1280, height: 900 });
	await page.goto(baseUrl, { waitUntil: 'networkidle0' });
	await page.waitForSelector('footer.horizon .band .bottom');
	const canvasBlocked = await page.$eval(
		'footer.horizon .particle-sea',
		(sea) => sea.querySelector('canvas') === null && !sea.hasAttribute('data-engine')
	);
	assert.ok(canvasBlocked, 'the test must exercise the footer with its sea canvas unavailable');

	const desktop = await sampleCopyrightBackground(page, reader);
	assert.ok(
		contrastRatio(desktop.foreground, desktop.background) >= 4.5,
		`desktop copyright must contrast with its canvas-failure fallback (fg ${desktop.foreground}, bg ${desktop.background})`
	);
	const desktopScale = await page.$eval('footer.horizon .particle-sea', (sea) =>
		Number.parseFloat(getComputedStyle(sea).getPropertyValue('--sea-scale'))
	);

	await page.setViewport({ width: 390, height: 844 });
	await page.evaluate(
		() =>
			new Promise((resolveFrame) =>
				requestAnimationFrame(() => requestAnimationFrame(resolveFrame))
			)
	);
	const phone = await sampleCopyrightBackground(page, reader);
	const phoneScale = await page.$eval('footer.horizon .particle-sea', (sea) =>
		Number.parseFloat(getComputedStyle(sea).getPropertyValue('--sea-scale'))
	);
	assert.notEqual(phoneScale, desktopScale, 'the CSS fallback sea scale follows the phone resize');
	assert.ok(
		contrastRatio(phone.foreground, phone.background) >= 4.5,
		`phone copyright must contrast with its canvas-failure fallback (fg ${phone.foreground}, bg ${phone.background})`
	);
	assert.equal(
		liveEngineRun?.contextCalls.filter((call) => call.contextType === 'webgl2').length,
		1,
		`one WebGL2 engine attempt per footer load; a 2D fallback is a separate canvas and is allowed: ${JSON.stringify(liveEngineRun)}`
	);

	await page.waitForFunction(() => {
		const link = document.querySelector('footer.horizon .trust a[href*="/privacy"]');
		return link && !link.closest('[inert]') && !link.closest('[aria-hidden="true"]');
	});
	await page.focus('footer.horizon .trust a[href*="/privacy"]');
	await page.keyboard.press('Tab');
	const focus = await page.$eval('footer.horizon .to-top', (button) => {
		const style = getComputedStyle(button);
		return {
			active: document.activeElement === button,
			visible: button.matches(':focus-visible'),
			color: style.color,
			outlineColor: style.outlineColor,
			outlineStyle: style.outlineStyle
		};
	});
	assert.equal(
		focus.active,
		true,
		'Tab from the final trust link must focus the back-to-top button'
	);
	assert.equal(focus.visible, true, 'keyboard navigation must match :focus-visible');
	assert.equal(focus.outlineStyle, 'solid', 'keyboard focus must show an outline');
	assert.equal(focus.outlineColor, focus.color, 'focus outline must follow currentColor');

	console.log('Footer contrast and keyboard-focus regression checks passed.');
} finally {
	if (browser) await browser.close();
	if (server)
		await new Promise((resolveClose, reject) =>
			server.close((error) => (error ? reject(error) : resolveClose()))
		);
}
