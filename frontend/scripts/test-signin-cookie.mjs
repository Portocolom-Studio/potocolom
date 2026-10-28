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

async function signIn(page, email) {
	await page.waitForSelector('#login-email');
	await page.waitForFunction(() => !document.activeViewTransition, { timeout: 2000 });
	await page.$eval('#login-email', (input) => {
		input.value = '';
	});
	await page.type('#login-email', email);
	await page.$eval('#login-password', (input) => {
		input.value = '';
	});
	await page.type('#login-password', PASSWORD);
	await page.click('button[type="submit"]');
	await page.waitForFunction(() => location.pathname === '/app', { timeout: WAIT_MS });
	await page.waitForFunction(() => document.querySelector('[data-studio-shell]'), {
		timeout: WAIT_MS
	});
}

async function signOut(page, email) {
	await page.evaluate((selectedEmail) => {
		const button = [...document.querySelectorAll('button')].find((item) =>
			item.textContent?.includes(selectedEmail)
		);
		if (!button) throw new Error('account menu button not found');
		button.click();
	}, email);
	await page.waitForSelector('[role="menuitem"]');
	await page.evaluate(() => {
		const item = [...document.querySelectorAll('[role="menuitem"]')].find(
			(entry) => entry.textContent?.trim() === 'Log out'
		);
		if (!item) throw new Error('Log out menu item not found');
		item.click();
	});
	await page.waitForFunction(() => location.pathname === '/login', { timeout: WAIT_MS });
}

async function clickButtonText(page, label) {
	await page.evaluate((selectedLabel) => {
		const button = [...document.querySelectorAll('button')].find(
			(item) => item.textContent?.trim() === selectedLabel
		);
		if (!button) throw new Error(`button not found: ${selectedLabel}`);
		button.click();
	}, label);
}

async function accountId(page) {
	const account = await page.evaluate(async () => {
		const response = await fetch('/api/v1/account', { credentials: 'include' });
		return response.ok ? response.json() : null;
	});
	assert.match(
		account?.id ?? '',
		/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
	);
	return account.id.toLowerCase();
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
			await page.evaluateOnNewDocument(() => {
				window.__signinDocumentMarker = crypto.randomUUID();
			});
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

			const documentMarker = await page.evaluate(() => window.__signinDocumentMarker);
			const ownerA = await accountId(page);
			const invitation = await page.evaluate(async (email) => {
				const token = document.cookie.match(/(?:^|;\s*)potocolom_csrf=([^;]*)/)?.[1];
				const response = await fetch('/api/v1/invitations', {
					method: 'POST',
					credentials: 'include',
					headers: { 'Content-Type': 'application/json', 'x-csrf-token': token ?? '' },
					body: JSON.stringify({ email, role: 'user' })
				});
				return { status: response.status, body: await response.json() };
			}, 'member@example.com');
			assert.equal(invitation.status, 201, 'account A can create a real account B invitation');
			const invitationToken = invitation.body.token;
			assert.ok(invitationToken);
			const imageSelection = '550e8400-e29b-41d4-a716-446655440002';
			await page.evaluate(
				({ ownerId, selectedId }) => {
					localStorage.setItem(
						`potocolom-view-v1:account-${ownerId}:metrics`,
						JSON.stringify({
							version: 1,
							section: 'metrics',
							value: {
								tab: 'benchmarks',
								usageRange: '24h',
								mineRange: '7d',
								selectedSessionId: '2026-10-27T12:00:00Z'
							}
						})
					);
					localStorage.setItem(
						`potocolom-view-v1:account-${ownerId}:images`,
						JSON.stringify({
							version: 1,
							section: 'images',
							value: {
								mode: 'grid',
								filters: { models: [], sizes: [], range: 'all', from: '', through: '' },
								action: 'all',
								search: 'owner-a-only',
								starredOnly: false,
								gridAnchor: null,
								gridSelectionId: selectedId,
								lineageSelection: null
							}
						})
					);
				},
				{ ownerId: ownerA, selectedId: imageSelection }
			);

			await signOut(page, EMAIL);
			await page.evaluate((token) => {
				const link = document.createElement('a');
				link.href = `/join#${token}`;
				document.body.append(link);
				link.click();
				link.remove();
			}, invitationToken);
			await page.waitForSelector('#join-password');
			await page.type('#join-password', PASSWORD);
			await page.type('#join-confirm', PASSWORD);
			await page.click('button[type="submit"]');
			await page.waitForFunction(() => location.pathname === '/app', { timeout: WAIT_MS });
			const ownerB = await accountId(page);
			assert.notEqual(ownerB, ownerA, 'the second sign-in must have a different account UUID');
			assert.equal(
				await page.evaluate(() => window.__signinDocumentMarker),
				documentMarker,
				'account changes stay in the same SPA document'
			);
			const bImagePrefs = await page.evaluate((ownerId) => {
				const raw = localStorage.getItem(`potocolom-view-v1:account-${ownerId}:images`);
				return raw === null ? null : JSON.parse(raw);
			}, ownerB);
			assert.ok(bImagePrefs === null || bImagePrefs.value.search !== 'owner-a-only');
			await clickButtonText(page, 'Images');
			await page.waitForSelector('input[aria-label="Search prompts"]');
			assert.equal(
				await page.$eval('input[aria-label="Search prompts"]', (input) => input.value),
				'',
				'account B starts with its own empty Images search'
			);
			assert.equal(
				await page.evaluate(
					async () => (await fetch('/api/v1/users', { credentials: 'include' })).status
				),
				403,
				'the role check still refuses account B an admin endpoint'
			);

			await signOut(page, 'member@example.com');
			await signIn(page, EMAIL);
			assert.equal(await accountId(page), ownerA, 'the original account UUID returns');
			assert.equal(
				await page.evaluate(() => window.__signinDocumentMarker),
				documentMarker,
				'returning to account A does not reload the browser document'
			);
			await clickButtonText(page, 'Images');
			await page.waitForSelector('input[aria-label="Search prompts"]');
			await page.waitForFunction(
				() => document.querySelector('input[aria-label="Search prompts"]')?.value === 'owner-a-only'
			);
			assert.equal(
				await page.$eval('input[aria-label="Search prompts"]', (input) => input.value),
				'owner-a-only'
			);
			await clickButtonText(page, 'Metrics');
			await page.waitForFunction(
				() => new URL(location.href).searchParams.get('view') === 'metrics'
			);
			assert.equal(
				new URL(page.url()).searchParams.get('tab'),
				'benchmarks',
				'account A restores its allowed Metrics tab'
			);

			const resetRequest = waitForUnsafeRequest(page, '/api/v1/auth/reset');
			await page.goto(`${publicUrl}/reset`, { waitUntil: 'networkidle0' });
			await page.waitForSelector('#reset-email');
			await page.type('#reset-email', EMAIL);
			const currentCsrfCookie = await page.evaluate(
				() => document.cookie.match(/(?:^|;\s*)potocolom_csrf=([^;]*)/)?.[1] ?? null
			);
			assert.ok(currentCsrfCookie, 'the active account must have a readable CSRF cookie');
			await page.click('button[type="submit"]');
			const request = await resetRequest;
			const sent = request.headers()['x-csrf-token'];
			assert.ok(sent, 'the shipped apiFetch must send x-csrf-token');
			assert.ok(sent === currentCsrfCookie, 'x-csrf-token must match the active CSRF cookie');
		} finally {
			await browser.close();
		}
	} finally {
		await stopApi(api);
		await runPython(`DROP DATABASE IF EXISTS "${databaseName}" WITH (FORCE)`, adminUrl);
	}
});
