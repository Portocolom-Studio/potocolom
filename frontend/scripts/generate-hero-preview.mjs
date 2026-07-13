#!/usr/bin/env node
/**
 * Capture seeded LatentCanvas frames for static assets.
 * Requires a local Chrome/Chromium (see PUPPETEER_EXECUTABLE_PATH).
 */
import { spawn } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, '..');
const staticDir = join(frontendRoot, 'static');
const viteBin = join(frontendRoot, 'node_modules', 'vite', 'bin', 'vite.js');
const port = Number(process.env.HERO_PREVIEW_PORT ?? 5199);
const chromePath =
	process.env.PUPPETEER_EXECUTABLE_PATH ?? process.env.CHROME_PATH ?? '/usr/bin/google-chrome';

const targets = [
	{ name: 'og.png', w: 1200, h: 630 },
	{ name: 'hero-poster.png', w: 1920, h: 1080 }
];

function waitForServer(url, timeoutMs = 60_000) {
	const start = Date.now();
	return new Promise((resolve, reject) => {
		const tick = async () => {
			try {
				const response = await fetch(url);
				if (response.ok) return resolve();
			} catch {
				// server not up yet
			}
			if (Date.now() - start > timeoutMs) {
				reject(new Error(`Timed out waiting for ${url}`));
				return;
			}
			setTimeout(tick, 250);
		};
		tick();
	});
}

function startDevServer() {
	const child = spawn(
		process.execPath,
		[viteBin, 'dev', '--host', '127.0.0.1', '--port', String(port)],
		{
			cwd: frontendRoot,
			stdio: ['ignore', 'pipe', 'pipe'],
			env: { ...process.env, PUBLIC_WAITLIST_URL: '' }
		}
	);

	let log = '';
	child.stdout?.on('data', (chunk) => {
		log += chunk.toString();
	});
	child.stderr?.on('data', (chunk) => {
		log += chunk.toString();
	});

	return { child, log: () => log };
}

async function stopDevServer(child) {
	if (!child || child.killed) return;

	await new Promise((resolve) => {
		const timer = setTimeout(() => {
			try {
				child.kill('SIGKILL');
			} catch {
				// already gone
			}
			resolve();
		}, 2_000);

		child.once('exit', () => {
			clearTimeout(timer);
			resolve();
		});

		child.kill('SIGTERM');
	});
}

async function capture(browser, target) {
	const page = await browser.newPage();
	const url = `http://127.0.0.1:${port}/hero-preview?w=${target.w}&h=${target.h}&seed=42`;

	await page.setViewport({ width: target.w, height: target.h, deviceScaleFactor: 1 });
	await page.goto(url, { waitUntil: 'networkidle2', timeout: 60_000 });
	await page.waitForFunction('window.__heroPreviewReady === true', { timeout: 60_000 });

	const dataUrl = await page.$eval('canvas', (node) => {
		const canvas = node;
		if (!(canvas instanceof HTMLCanvasElement)) {
			throw new Error('Expected a canvas element');
		}
		return canvas.toDataURL('image/png');
	});

	await page.close();

	const base64 = dataUrl.split(',')[1];
	if (!base64) throw new Error(`Canvas export failed for ${target.name}`);
	return Buffer.from(base64, 'base64');
}

async function main() {
	await mkdir(staticDir, { recursive: true });

	const { child, log } = startDevServer();
	let browser;

	try {
		await waitForServer(`http://127.0.0.1:${port}/hero-preview`);
		browser = await puppeteer.launch({
			executablePath: chromePath,
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});

		for (const target of targets) {
			const buffer = await capture(browser, target);
			const outPath = join(staticDir, target.name);
			await writeFile(outPath, buffer);
			console.log(`Wrote ${outPath} (${target.w}x${target.h}, ${buffer.length} bytes)`);
		}
	} catch (error) {
		console.error(log());
		throw error;
	} finally {
		if (browser) await browser.close();
		await stopDevServer(child);
	}
}

main()
	.then(() => process.exit(0))
	.catch((error) => {
		console.error(error);
		process.exit(1);
	});
