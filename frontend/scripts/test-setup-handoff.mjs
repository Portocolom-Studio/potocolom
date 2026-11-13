import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { constants } from 'node:fs';
import { access } from 'node:fs/promises';
import { createServer } from 'node:net';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';
import { newStudioPage } from './studio-test-page.mjs';

// The real handoff: a backend on a fresh database, a setup link minted by the
// operator command, and the built studio in a browser. Each install gets its
// own database and its own browser context, so a session from one never
// reaches another, even though they share a host.
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
const ADMIN = {
	email: 'ana@example.com',
	password: 'a real password for the handoff test 12345'
};

const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));

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
	throw new Error('No Chrome executable found. Set PUPPETEER_EXECUTABLE_PATH or CHROME_PATH.');
}

async function runPython(sql, connectUrl) {
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

async function freePort() {
	const server = createServer();
	await new Promise((resolveListen) => server.listen(0, '127.0.0.1', resolveListen));
	const { port } = server.address();
	await new Promise((resolveClose) => server.close(resolveClose));
	return port;
}

function installEnv(databaseUrl, publicUrl) {
	const env = {
		...process.env,
		DATABASE_URL: databaseUrl,
		ROOT_KEYS: `1:${randomBytes(32).toString('base64')}`,
		PUBLIC_URL: publicUrl,
		ALLOWED_ORIGINS: publicUrl,
		FRONTEND_DIST: build,
		FLEET_TOKEN_KEY: randomBytes(32).toString('hex'),
		TELEMETRY: 'false',
		EMAIL_BACKEND: 'none'
	};
	// Nothing may decide the install ahead of the link: the setup page is what
	// is under test.
	delete env.AUTH_MODE;
	return env;
}

// The link the operator command prints, the same one the installer shows.
async function mintSetupLink(env) {
	const output = await new Promise((resolveRun, rejectRun) => {
		const child = spawn(python, ['-m', 'app.operator', 'setup-link'], {
			cwd: backend,
			env,
			stdio: ['ignore', 'pipe', 'inherit']
		});
		let stdout = '';
		child.stdout.on('data', (chunk) => (stdout += chunk));
		child.on('error', rejectRun);
		child.on('exit', (code) =>
			code === 0 ? resolveRun(stdout) : rejectRun(new Error(`app.operator exited ${code}`))
		);
	});
	const link = output.trim().split('\n').at(-1);
	assert.match(link, /\/setup#[A-Za-z0-9_-]{32,}$/, 'the operator printed a setup link');
	return link;
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

async function stopApi(api) {
	if (!api || api.exitCode !== null) return;
	api.kill('SIGTERM');
	const exited = await Promise.race([
		new Promise((resolveExit) => api.once('exit', () => resolveExit(true))),
		pause(5000).then(() => false)
	]);
	if (!exited) api.kill('SIGKILL');
}

// One fresh, unclaimed install: a database, a backend on it, and a link to
// claim it. The database and the API go away when the run ends.
async function withFreshInstall(run) {
	const name = `potocolom_handoff_${process.pid}_${randomBytes(4).toString('hex')}`;
	const databaseUrl = new URL(adminUrl);
	databaseUrl.pathname = `/${name}`;
	await runPython(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`, adminUrl);
	await runPython(`CREATE DATABASE "${name}"`, adminUrl);
	let api;
	let started;
	try {
		// freePort() closes its probe socket before uvicorn binds, so another job
		// can take the port. Pick, mint, start and wait again, at most 3 times.
		for (let attempt = 1; ; attempt += 1) {
			const port = await freePort();
			const publicUrl = `http://127.0.0.1:${port}`;
			const env = installEnv(databaseUrl.toString(), publicUrl);
			const link = await mintSetupLink(env);
			const apiLog = [];
			api = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
				cwd: backend,
				env,
				stdio: ['ignore', 'pipe', 'pipe']
			});
			api.stdout.on('data', (chunk) => apiLog.push(chunk));
			api.stderr.on('data', (chunk) => apiLog.push(chunk));
			try {
				await waitForConfig(publicUrl, api, apiLog);
				started = { publicUrl, link };
				break;
			} catch {
				if (attempt === 3)
					throw new Error(`API would not start after 3 attempts\n${apiLog.join('')}`);
				await stopApi(api);
			}
		}
		return await run(started);
	} finally {
		await stopApi(api);
		await runPython(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`, adminUrl);
	}
}

async function openLink(context, link) {
	const page = await newStudioPage(context);
	page.setDefaultTimeout(WAIT_MS);
	const errors = [];
	page.on('pageerror', (error) => errors.push(error.stack ?? error.message));
	await page.goto(link, { waitUntil: 'domcontentloaded' });
	return { page, errors };
}

async function waitForText(page, text) {
	await page.waitForFunction((needle) => document.body.innerText.includes(needle), {}, text);
}

async function waitForButton(page, label) {
	await page.waitForFunction(
		(selectedLabel) =>
			[...document.querySelectorAll('button')].some(
				(item) => item.textContent?.trim() === selectedLabel
			),
		{},
		label
	);
}

async function clickButton(page, label) {
	await waitForButton(page, label);
	await page.evaluate((selectedLabel) => {
		const button = [...document.querySelectorAll('button')].find(
			(item) => item.textContent?.trim() === selectedLabel
		);
		button.click();
	}, label);
}

// What the browser's own session says, read by the page with its cookies.
async function accountOf(page) {
	return page.evaluate(async () => {
		const response = await fetch('/api/v1/account', { credentials: 'include' });
		return { status: response.status, body: response.ok ? await response.json() : null };
	});
}

async function testAdminClaimReachesTheStudio(browser) {
	await withFreshInstall(async ({ link }) => {
		const context = await browser.createBrowserContext();
		try {
			const { page, errors } = await openLink(context, link);
			await waitForButton(page, 'Create an admin account');
			assert.equal(await page.evaluate(() => location.hash), '', 'the link left the address bar');
			await clickButton(page, 'Create an admin account');
			await page.type('#setup-email', ADMIN.email);
			await page.type('#setup-password', ADMIN.password);
			await page.type('#setup-confirm', ADMIN.password);
			await clickButton(page, 'Create admin account');
			await waitForText(page, 'Setup is complete.');
			await page.click('a[href="/app"]');
			await page.waitForFunction(() => location.pathname === '/app');
			await waitForText(page, ADMIN.email);
			const account = await accountOf(page);
			assert.equal(account.status, 200, 'the claim left the browser signed in');
			assert.equal(account.body.email, ADMIN.email);
			assert.equal(account.body.role, 'admin');
			const config = await page.evaluate(async () =>
				(await fetch('/api/v1/config?setup=true')).json()
			);
			assert.equal('setup_claimed' in config, false);
			const spent = await page.evaluate(async (token) => {
				const response = await fetch('/api/v1/auth/setup/status', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ token })
				});
				return response.status;
			}, new URL(link).hash.slice(1));
			assert.equal(spent, 403);
			assert.deepEqual(errors, [], 'the setup page raised no errors');
			await page.close();
		} finally {
			await context.close();
		}
	});
}

// Chooses no accounts on the page the link opened, and waits for the studio.
async function chooseNoAccounts(page) {
	await waitForButton(page, 'Use without accounts');
	await clickButton(page, 'Use without accounts');
	await clickButton(page, 'Continue without accounts');
	await waitForText(page, 'Models are next');
	await page.click('a[href="/app"]');
	await page.waitForFunction(() => location.pathname === '/app');
	await page.waitForFunction(() => document.querySelector('[data-studio-shell]') !== null);
	await waitForButton(page, 'Settings');
}

async function testNoAccountsOpensLocally(browser) {
	await withFreshInstall(async ({ link }) => {
		const context = await browser.createBrowserContext();
		try {
			const { page, errors } = await openLink(context, link);
			await chooseNoAccounts(page);
			assert.equal(await page.evaluate(() => location.pathname), '/app', 'no sign-in redirect');
			const local = await accountOf(page);
			assert.equal(local.status, 404, 'a no-accounts install has no signed-in account');
			assert.deepEqual(errors, [], 'the setup page raised no errors');
			await page.close();
		} finally {
			await context.close();
		}
	});
}

async function testStaleTabAfterNoneChoice(browser) {
	await withFreshInstall(async ({ link }) => {
		const context = await browser.createBrowserContext();
		try {
			const first = await openLink(context, link);
			await waitForButton(first.page, 'Use without accounts');
			const second = await openLink(context, link);
			await waitForButton(second.page, 'Use without accounts');
			await first.page.bringToFront();
			await clickButton(first.page, 'Use without accounts');
			await clickButton(first.page, 'Continue without accounts');
			await waitForText(first.page, 'Models are next');
			await second.page.bringToFront();
			await clickButton(second.page, 'Create an admin account');
			await second.page.type('#setup-email', ADMIN.email);
			await second.page.type('#setup-password', ADMIN.password);
			await second.page.type('#setup-confirm', ADMIN.password);
			await clickButton(second.page, 'Create admin account');
			await waitForText(second.page, 'This install is already set up.');
			assert.notEqual(await second.page.$('a[href="/app"]'), null);
			assert.deepEqual(first.errors, []);
			assert.deepEqual(second.errors, []);
		} finally {
			await context.close();
		}
	});
}

async function testSettingsTurnsAccountsOn(browser) {
	await withFreshInstall(async ({ link }) => {
		const context = await browser.createBrowserContext();
		try {
			const { page, errors } = await openLink(context, link);
			await chooseNoAccounts(page);
			await clickButton(page, 'Settings');
			await page.waitForSelector('#turn-on-email');
			// The view change crossfades; the snapshot overlay takes the clicks until it ends.
			await page.waitForFunction(() => !document.activeViewTransition, { timeout: 2000 });
			await page.type('#turn-on-email', ADMIN.email);
			await page.type('#turn-on-password', ADMIN.password);
			await page.click('#turn-on-confirm');
			const form = await page.evaluate(() => ({
				checked: document.querySelector('#turn-on-confirm')?.checked ?? null,
				email: document.querySelector('#turn-on-email')?.value ?? null,
				password: (document.querySelector('#turn-on-password')?.value ?? '').length,
				submitDisabled:
					[...document.querySelectorAll('button')].find(
						(item) => item.textContent?.trim() === 'Turn on accounts'
					)?.disabled ?? null
			}));
			assert.deepEqual(
				{ checked: form.checked, submitDisabled: form.submitDisabled },
				{ checked: true, submitDisabled: false },
				`the turn-on form is ready: ${JSON.stringify(form)}`
			);
			const enabled = page.waitForResponse((response) =>
				response.url().endsWith('/api/v1/auth/enable')
			);
			await clickButton(page, 'Turn on accounts');
			const answer = await enabled;
			const refusal = answer.status() === 204 ? '' : await answer.text();
			assert.equal(answer.status(), 204, `the turn-on answered ${answer.status()}: ${refusal}`);
			await page.waitForFunction(
				() =>
					location.pathname === '/app' &&
					location.search === '' &&
					!document.querySelector('#turn-on-email')
			);
			await waitForText(page, ADMIN.email);
			const account = await accountOf(page);
			assert.equal(account.status, 200, 'the turn-on left the browser signed in');
			assert.equal(account.body.email, ADMIN.email);
			assert.equal(account.body.role, 'admin');
			assert.deepEqual(errors, [], 'the setup page raised no errors');
			await page.close();
		} finally {
			await context.close();
		}
	});
}

async function main() {
	const failures = [];
	let browser;
	const cases = [
		[
			'an admin account claimed through the link reaches the studio',
			testAdminClaimReachesTheStudio
		],
		['no accounts chosen through the link opens the studio locally', testNoAccountsOpensLocally],
		['a stale tab recovers after no accounts was chosen', testStaleTabAfterNoneChoice],
		['settings turns accounts on for a no-accounts install', testSettingsTurnsAccountsOn]
	];
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-gpu']
		});
		for (const [name, run] of cases) {
			try {
				await run(browser);
				console.log(`ok - ${name}`);
			} catch (error) {
				failures.push(name);
				console.error(`not ok - ${name}\n${error.stack ?? error}`);
			}
		}
	} finally {
		await browser?.close();
	}
	if (failures.length > 0) {
		throw new Error(`${failures.length} setup handoff check(s) failed`);
	}
}

await main();
