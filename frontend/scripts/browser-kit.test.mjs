// Behaviour of the build server and Chrome launcher that every browser check imports.
import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';
import { launchChrome, serveBuild } from './browser-kit.mjs';

const chromeVars = ['PUPPETEER_EXECUTABLE_PATH', 'CHROME_PATH', 'CHROME_BIN'];
const exactTypes = [
	['index.html', 'text/html'],
	['app.js', 'application/javascript'],
	['app.css', 'text/css'],
	['data.json', 'application/json'],
	['logo.svg', 'image/svg+xml'],
	['pixel.png', 'image/png'],
	['photo.webp', 'image/webp'],
	['face.woff2', 'font/woff2'],
	['favicon.ico', 'image/x-icon'],
	['notes.bin', 'application/octet-stream']
];

let root;
let build;
let chrome;

before(async () => {
	root = await mkdtemp(join(tmpdir(), 'browser-kit-'));
	build = join(root, 'build');
	await mkdir(join(build, 'docs'), { recursive: true });
	await writeFile(join(build, 'index.html'), 'home page');
	await writeFile(join(build, 'about.html'), 'about page');
	await writeFile(join(build, '404.html'), 'not found page');
	await writeFile(join(build, 'docs', 'index.html'), 'docs index');
	await writeFile(join(build, 'app.js'), 'console.log(1);');
	await writeFile(join(build, 'app.css'), 'body {}');
	await writeFile(join(build, 'data.json'), '{}');
	await writeFile(join(build, 'logo.svg'), '<svg xmlns="http://www.w3.org/2000/svg"/>');
	await writeFile(join(build, 'pixel.png'), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
	await writeFile(join(build, 'photo.webp'), 'webp');
	await writeFile(join(build, 'face.woff2'), 'woff2');
	await writeFile(join(build, 'favicon.ico'), 'ico');
	await writeFile(join(build, 'notes.bin'), 'bin');
	await writeFile(join(root, 'secret.txt'), 'SECRET');
	await mkdir(join(root, 'build-evil'));
	await writeFile(join(root, 'build-evil', 'secret.txt'), 'SECRET');

	// Fake Chrome binaries: each records its own path and argv, then exits, so launch fails after starting.
	for (const name of ['chrome-a', 'chrome-b', 'chrome-c']) {
		const path = join(root, name);
		await writeFile(
			path,
			`#!/bin/sh\nprintf '%s\\n' "$0" "$@" >> '${join(root, 'launches.log')}'\nexit 1\n`
		);
		await chmod(path, 0o755);
	}
	chrome = Object.fromEntries(['a', 'b', 'c'].map((name) => [name, join(root, `chrome-${name}`)]));
});

after(() => rm(root, { recursive: true, force: true }));

async function withServer(options, run) {
	const { server, baseUrl } = await serveBuild(build, options);
	try {
		return await run(baseUrl);
	} finally {
		server.closeAllConnections();
		server.close();
	}
}

async function get(baseUrl, path) {
	const response = await fetch(new URL(path, baseUrl));
	return {
		status: response.status,
		type: response.headers.get('content-type')?.split(';')[0].trim(),
		body: await response.text()
	};
}

test('serveBuild returns its server and a base URL on 127.0.0.1', async () => {
	const { server, baseUrl } = await serveBuild(build);
	try {
		assert.equal(baseUrl, `http://127.0.0.1:${server.address().port}`);
	} finally {
		server.closeAllConnections();
		server.close();
	}
});

test('an exact file is served with its content type', async () => {
	await withServer({}, async (baseUrl) => {
		for (const [file, type] of exactTypes) {
			const response = await get(baseUrl, `/${file}`);
			assert.equal(response.status, 200, file);
			assert.equal(response.type, type, file);
		}
		const query = await get(baseUrl, '/index.html?v=2');
		assert.equal(query.status, 200);
		assert.equal(query.body, 'home page');
	});
});

test('a clean URL resolves to .html and a directory to its index.html', async () => {
	await withServer({}, async (baseUrl) => {
		assert.equal((await get(baseUrl, '/about')).body, 'about page');
		assert.equal((await get(baseUrl, '/docs/')).body, 'docs index');
		assert.equal((await get(baseUrl, '/docs')).body, 'docs index');
	});
});

test('a missing path is a 404 when no fallback is configured', async () => {
	await withServer({}, async (baseUrl) => {
		assert.equal((await get(baseUrl, '/missing/page')).status, 404);
	});
});

test('a missing path gets the configured fallback page', async () => {
	await withServer({ fallback: 'index.html' }, async (baseUrl) => {
		assert.equal((await get(baseUrl, '/studio/models/42')).body, 'home page');
	});
	await withServer({ fallback: '404.html' }, async (baseUrl) => {
		assert.equal((await get(baseUrl, '/missing')).body, 'not found page');
	});
});

test('a path that escapes the build directory is refused, even with a fallback', async () => {
	await withServer({ fallback: 'index.html' }, async (baseUrl) => {
		for (const path of ['/..%2fsecret.txt', '/..%2fbuild-evil%2fsecret.txt']) {
			const response = await get(baseUrl, path);
			assert.equal(response.status, 404, path);
			assert.doesNotMatch(response.body, /SECRET/, path);
		}
	});
});

test('the api hook answers first and can hold a response until a later request', async () => {
	let release;
	const released = new Promise((done) => {
		release = done;
	});
	const api = async (request, response) => {
		const { pathname } = new URL(request.url, 'http://localhost');
		if (pathname === '/api/ping') {
			response.writeHead(200, { 'Content-Type': 'application/json' }).end('{"ok":true}');
			return true;
		}
		if (pathname === '/api/hold') {
			await released;
			response.writeHead(200, { 'Content-Type': 'text/plain' }).end('held');
			return true;
		}
		if (pathname === '/api/release') {
			release();
			response.writeHead(204).end();
			return true;
		}
		return false;
	};
	await withServer({ api }, async (baseUrl) => {
		assert.equal((await get(baseUrl, '/api/ping')).body, '{"ok":true}');
		const held = get(baseUrl, '/api/hold');
		assert.equal((await get(baseUrl, '/api/release')).status, 204);
		assert.equal((await held).body, 'held');
		// A request the hook declines falls through to the static lookup.
		assert.equal((await get(baseUrl, '/about')).body, 'about page');
		assert.equal((await get(baseUrl, '/api/unknown')).status, 404);
	});
});

// Runs launchChrome with the given env and reports which executable started and with which args.
async function startedWith(env, options = {}) {
	const saved = Object.fromEntries(chromeVars.map((name) => [name, process.env[name]]));
	for (const name of chromeVars) delete process.env[name];
	Object.assign(process.env, env);
	await rm(join(root, 'launches.log'), { force: true });
	let error = null;
	try {
		// The fake binary exits 1, so the launch rejects after the binary has started. A
		// real Chrome picked by mistake launches instead, and is closed so the run cannot hang.
		await launchChrome(options).then(
			(browser) => browser.close(),
			(reason) => {
				error = reason;
			}
		);
	} finally {
		for (const name of chromeVars) {
			if (saved[name] === undefined) delete process.env[name];
			else process.env[name] = saved[name];
		}
	}
	const log = await readFile(join(root, 'launches.log'), 'utf8').catch(() => '');
	return { lines: log.split('\n').filter(Boolean), error };
}

test('PUPPETEER_EXECUTABLE_PATH wins over CHROME_PATH and CHROME_BIN', async () => {
	const { lines, error } = await startedWith({
		PUPPETEER_EXECUTABLE_PATH: chrome.a,
		CHROME_PATH: chrome.b,
		CHROME_BIN: chrome.c
	});
	assert.equal(lines[0], chrome.a, `launch error: ${error?.message}`);
});

test('CHROME_PATH wins over CHROME_BIN', async () => {
	const { lines, error } = await startedWith({ CHROME_PATH: chrome.b, CHROME_BIN: chrome.c });
	assert.equal(lines[0], chrome.b, `launch error: ${error?.message}`);
});

test('CHROME_BIN is used when the other variables are unset', async () => {
	const { lines, error } = await startedWith({ CHROME_BIN: chrome.c });
	assert.equal(lines[0], chrome.c, `launch error: ${error?.message}`);
});

test('a candidate that points at a missing file is skipped', async () => {
	const { lines, error } = await startedWith({
		PUPPETEER_EXECUTABLE_PATH: join(root, 'missing-chrome'),
		CHROME_PATH: chrome.b
	});
	assert.equal(lines[0], chrome.b, `launch error: ${error?.message}`);
});

test('launchChrome adds --no-sandbox and keeps the caller args', async () => {
	const { lines, error } = await startedWith(
		{ CHROME_PATH: chrome.a },
		{ args: ['--window-size=800,600'] }
	);
	assert.ok(lines.includes('--no-sandbox'), `launch error: ${error?.message}`);
	assert.ok(lines.includes('--window-size=800,600'));
});
