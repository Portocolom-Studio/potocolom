import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import { access } from 'node:fs/promises';
import { createServer } from 'node:net';
import { randomBytes } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import puppeteer from 'puppeteer-core';

const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
const backend = resolve(fileURLToPath(new URL('../../backend', import.meta.url)));
const python =
	process.env.PYTHON ??
	resolve(fileURLToPath(new URL('../../backend/.venv/bin/python', import.meta.url)));
if (
	!(await access(python, constants.X_OK).then(
		() => true,
		() => false
	))
)
	throw new Error(`backend venv missing at ${python}; run make setup first`);
const adminUrl =
	process.env.POSTGRES_ADMIN_URL ?? 'postgresql://potocolom:potocolom@localhost:5432/postgres';
const WAIT_MS = 15000;
const EMAIL = 'admin@example.com';
const PASSWORD = 'a real password for the sign-in test 12345';

const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));

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

async function runPython(sql, connectUrl) {
	// The backend already depends on asyncpg (backend/pyproject.toml), so the
	// throwaway database needs no Postgres client installed for node.
	await new Promise((resolveRun, rejectRun) => {
		const child = spawn(
			python,
			[
				'-c',
				'import asyncio; import sys; import asyncpg\n' +
					'async def main():\n' +
					'    connection = await asyncpg.connect(sys.argv[1])\n' +
					'    try:\n' +
					'        await connection.execute(sys.argv[2])\n' +
					'    finally:\n' +
					'        await connection.close()\n' +
					'asyncio.run(main())\n',
				connectUrl,
				sql
			],
			{ stdio: ['ignore', 'inherit', 'inherit'] }
		);
		child.on('error', rejectRun);
		child.on('exit', (code) =>
			code === 0 ? resolveRun() : rejectRun(new Error(`python exited ${code}`))
		);
	});
}

async function mintSetupToken(env) {
	const output = await new Promise((resolveRun, rejectRun) => {
		const child = spawn(python, ['-m', 'app.enable'], {
			cwd: backend,
			env,
			stdio: ['ignore', 'pipe', 'inherit']
		});
		let stdout = '';
		child.stdout.on('data', (chunk) => (stdout += chunk));
		child.on('error', rejectRun);
		child.on('exit', (code) =>
			code === 0 ? resolveRun(stdout) : rejectRun(new Error(`app.enable exited ${code}`))
		);
	});
	const match = output.match(/"token"\s*:\s*"([^"]+)"/);
	assert.ok(match, 'app.enable printed a setup token');
	return match[1];
}

async function freePort() {
	const server = createServer();
	await new Promise((resolveListen) => server.listen(0, '127.0.0.1', resolveListen));
	const { port } = server.address();
	await new Promise((resolveClose) => server.close(resolveClose));
	return port;
}

async function waitForConfig(publicUrl, api, apiLog) {
	const deadline = Date.now() + WAIT_MS;
	while (Date.now() < deadline) {
		if (api.exitCode !== null)
			throw new Error(`API exited early with code ${api.exitCode}\n${apiLog.join('')}`);
		try {
			const response = await fetch(`${publicUrl}/api/v1/config`);
			if (response.ok) return;
		} catch {
			// The API has not started listening yet.
		}
		await pause(200);
	}
	throw new Error(`API did not answer /api/v1/config\n${apiLog.join('')}`);
}

function waitForUnsafeRequest(page, url) {
	return new Promise((resolveRequest, rejectRequest) => {
		const timer = setTimeout(
			() => rejectRequest(new Error(`no unsafe request to ${url}`)),
			WAIT_MS
		);
		const onRequest = (request) => {
			if (request.method() !== 'POST' || !request.url().endsWith(url)) return;
			clearTimeout(timer);
			page.off('request', onRequest);
			resolveRequest(request);
		};
		page.on('request', onRequest);
	});
}

async function stopApi(api) {
	// An API that already exited is done; there is no 'exit' left to wait for.
	if (!api || api.exitCode !== null) return;
	api.kill('SIGTERM');
	const exited = await Promise.race([
		new Promise((resolveExit) => api.once('exit', () => resolveExit(true))),
		pause(5000).then(() => false)
	]);
	if (!exited) api.kill('SIGKILL');
}

test('a real browser signs in on the built /login and the next unsafe request carries the CSRF header', async () => {
	// A killed run may have left a database of the planned name behind, so drop
	// it with FORCE before creating today's; the suffix keeps concurrent runs
	// (one per self-hosted runner) from tripping over each other.
	const databaseName = `potocolom_signin_${process.pid}_${randomBytes(4).toString('hex')}`;
	const databaseUrl = new URL(adminUrl);
	databaseUrl.pathname = `/${databaseName}`;
	await runPython(`DROP DATABASE IF EXISTS "${databaseName}" WITH (FORCE)`, adminUrl);
	await runPython(`CREATE DATABASE "${databaseName}"`, adminUrl);
	let api;
	let token;
	let publicUrl;
	const apiLog = [];
	try {
		// freePort() closes its probe socket before uvicorn binds, and on a
		// shared runner another job can grab the port in between, which kills
		// the API. Retry the pick/start/wait on a fresh port, at most 3 times.
		for (let attempt = 1; ; attempt += 1) {
			const port = await freePort();
			publicUrl = `http://127.0.0.1:${port}`;
			const env = {
				...process.env,
				DATABASE_URL: databaseUrl.toString(),
				AUTH_MODE: 'accounts',
				ROOT_KEYS: `1:${randomBytes(32).toString('base64')}`,
				PUBLIC_URL: publicUrl,
				ALLOWED_ORIGINS: publicUrl,
				FRONTEND_DIST: build,
				FLEET_TOKEN_KEY: randomBytes(32).toString('hex'),
				TELEMETRY: 'false',
				EMAIL_BACKEND: 'none'
			};
			token = await mintSetupToken(env);
			api = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
				cwd: backend,
				env,
				stdio: ['ignore', 'pipe', 'pipe']
			});
			api.stdout.on('data', (chunk) => apiLog.push(chunk));
			api.stderr.on('data', (chunk) => apiLog.push(chunk));
			try {
				await waitForConfig(publicUrl, api, apiLog);
				break;
			} catch (error) {
				if (attempt === 3)
					throw new Error(`API would not start after 3 attempts\n${apiLog.join('')}`);
				await stopApi(api);
			}
		}

		const setup = await fetch(`${publicUrl}/api/v1/auth/setup`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ token, email: EMAIL, password: PASSWORD })
		});
		assert.equal(setup.status, 204, `setup claim answered ${setup.status}`);

		const browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-gpu']
		});
		try {
			const page = await browser.newPage();
			page.setDefaultTimeout(WAIT_MS);
			const client = await page.createCDPSession();
			// The claim set cookies on node's fetch, not on this browser, but
			// the browser starts signed out regardless: clear the store anyway.
			await client.send('Network.clearBrowserCookies');
			await page.goto(`${publicUrl}/login`, { waitUntil: 'networkidle0' });
			await page.waitForSelector('#login-email');
			await page.type('#login-email', EMAIL);
			await page.type('#login-password', PASSWORD);
			await page.click('button[type="submit"]');
			await page.waitForFunction(() => location.pathname === '/app', { timeout: WAIT_MS });

			const { cookies } = await client.send('Network.getAllCookies');
			// The page is plain HTTP, so the server issues the unprefixed
			// names: the __Host- prefix requires Secure, which a browser
			// refuses over HTTP. __Host-potocolom_session and
			// __Host-potocolom_csrf are the HTTPS form.
			const sessionCookie = cookies.find((cookie) => cookie.name === 'potocolom_session');
			const csrfCookie = cookies.find((cookie) => cookie.name === 'potocolom_csrf');
			assert.ok(sessionCookie, 'the browser must hold potocolom_session after sign-in');
			assert.ok(csrfCookie, 'the browser must hold potocolom_csrf after sign-in');
			assert.equal(sessionCookie.httpOnly, true, 'the session cookie must be HttpOnly');
			assert.equal(csrfCookie.httpOnly, false, 'the CSRF cookie must stay readable');
			assert.ok(sessionCookie.value, 'the session cookie must not be empty');
			assert.ok(csrfCookie.value, 'the CSRF cookie must not be empty');

			const resetRequest = waitForUnsafeRequest(page, '/api/v1/auth/reset');
			await page.goto(`${publicUrl}/reset`, { waitUntil: 'networkidle0' });
			await page.waitForSelector('#reset-email');
			await page.type('#reset-email', EMAIL);
			await page.click('button[type="submit"]');
			const request = await resetRequest;
			const sent = request.headers()['x-csrf-token'];
			assert.ok(sent, 'the shipped apiFetch must send x-csrf-token');
			assert.equal(sent, csrfCookie.value, 'x-csrf-token must match the CSRF cookie');
		} finally {
			await browser.close();
		}
	} finally {
		await stopApi(api);
		await runPython(`DROP DATABASE IF EXISTS "${databaseName}" WITH (FORCE)`, adminUrl);
	}
});
