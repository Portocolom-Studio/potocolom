// The static server and Chrome launcher that the browser checks share. The lint rule in
// eslint.config.js keeps createServer and puppeteer.launch calls for checks in this file.
import { constants } from 'node:fs';
import { access, readFile, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve, sep } from 'node:path';
import puppeteer from 'puppeteer-core';

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

async function firstFile(candidates) {
	for (const candidate of candidates) {
		if (
			await stat(candidate).then(
				(value) => value.isFile(),
				() => false
			)
		)
			return candidate;
	}
	return undefined;
}

// Serves dir on 127.0.0.1 and resolves to { server, baseUrl }. options.api(request, response)
// answers first when it returns true. Otherwise the path is looked up as a file, as .html, and
// as a directory index. A miss gets options.fallback (a file in dir, or a list tried in order,
// such as ['200.html', 'index.html'] for an app shell) or a 404.
export async function serveBuild(dir, { api, fallback } = {}) {
	const root = resolve(dir);
	const server = createServer(async (request, response) => {
		try {
			if (api && (await api(request, response))) return;
			const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
			const clean = resolve(root, `.${pathname}`);
			if (clean !== root && !clean.startsWith(root + sep)) {
				response.writeHead(404).end();
				return;
			}
			const candidates = [clean, `${clean}.html`, join(clean, 'index.html')];
			for (const name of [fallback].flat().filter(Boolean)) candidates.push(join(root, name));
			const found = await firstFile(candidates);
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
	return { server, baseUrl: `http://127.0.0.1:${server.address().port}` };
}

// The first Chrome that exists and is executable, or undefined when none is installed.
export async function findChrome() {
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
	return undefined;
}

// Headless Chrome with --no-sandbox, plus options.args. Other options go to puppeteer.launch.
export async function launchChrome({ args = [], ...options } = {}) {
	const executablePath = await findChrome();
	if (!executablePath)
		throw new Error('No Chrome executable found. Set PUPPETEER_EXECUTABLE_PATH or CHROME_PATH.');
	return puppeteer.launch({
		executablePath,
		headless: true,
		...options,
		args: ['--no-sandbox', ...args]
	});
}
