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

function assertOpaque(color, description) {
	assert.match(color, /^rgba?\(/, `${description} must resolve to an RGB color; got ${color}`);
	const alpha = color.match(/rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)/)?.[1];
	assert.ok(
		alpha === undefined || Number(alpha) === 1,
		`${description} must be opaque; got ${color}`
	);
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
	const page = await browser.newPage();
	let artworkFailed = false;
	await page.setRequestInterception(true);
	page.on('request', (request) => {
		if (request.url().includes('/images/horizon/sunrise-ascii.webp')) {
			artworkFailed = true;
			void request.abort();
		} else {
			void request.continue();
		}
	});

	await page.setViewport({ width: 1280, height: 900 });
	await page.goto(baseUrl, { waitUntil: 'networkidle0' });
	await page.waitForSelector('footer.horizon .band .bottom');
	assert.ok(artworkFailed, 'the test must exercise the footer with its horizon image unavailable');

	const desktop = await page.$eval('footer.horizon .band', (band) => {
		const footer = band.querySelector('.bottom');
		return {
			background: getComputedStyle(band).backgroundColor,
			foreground: getComputedStyle(footer).color
		};
	});
	assertOpaque(desktop.background, 'desktop footer fallback background');
	assert.ok(
		contrastRatio(desktop.foreground, desktop.background) >= 4.5,
		`desktop copyright must contrast with its image-failure fallback (fg ${desktop.foreground}, bg ${desktop.background})`
	);

	await page.setViewport({ width: 390, height: 844 });
	const phone = await page.$eval('footer.horizon .band', (band) => ({
		background: getComputedStyle(band).backgroundColor,
		foreground: getComputedStyle(band.querySelector('.bottom')).color
	}));
	assertOpaque(phone.background, 'phone footer fallback background');
	assert.ok(
		contrastRatio(phone.foreground, phone.background) >= 4.5,
		`phone copyright must contrast with its image-failure fallback (fg ${phone.foreground}, bg ${phone.background})`
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
