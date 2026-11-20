import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChrome, serveBuild } from './browser-kit.mjs';
import { newStudioPage } from './studio-test-page.mjs';

const messages = JSON.parse(
	readFileSync(new URL('../src/lib/i18n/en.json', import.meta.url), 'utf8')
);

// Mocked API, real build: /setup, the /app guard and the none-mode settings
// card against fixtures. Run after `PUBLIC_SITE_MODE= npm run build`.
const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
const timeoutMs = 15_000;
const token = 'Qx7_vB2-kL9mN4pR8sT1uW3yZ6aC0dE5fG7hJ9iK2lM'; // gitleaks:allow (made-up fixture token, not a credential)
const otherToken = 'Zk3_vB2-kL9mN4pR8sT1uW3yZ6aC0dE5fG7hJ9iK2nQ'; // gitleaks:allow (made-up fixture token, not a credential)
const password = 'a setup password of sixteen characters';
const unclaimedConfig = {
	auth_methods: [],
	setup_required: true,
	billing_enabled: false,
	languages: ['en', 'es'],
	banner: null
};
const noneConfig = { ...unclaimedConfig, setup_required: false };
const fixture = {};
const requests = [];
const pageErrors = [];
// Replies answered in order before the fixture defaults, so one test can make
// the first answer slow or fail and the next one succeed.
const queues = {};

const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));

function resetFixture() {
	fixture.config = { status: 200, body: unclaimedConfig };
	fixture.account = { status: 503, body: { detail: 'setup_required' } };
	fixture.setup = { status: 204 };
	fixture.status = { status: 200, body: { claimed: false, expires_in: 3120 } };
	fixture.enable = { status: 204 };
	queues['/api/v1/config'] = [];
	queues['/api/v1/auth/setup'] = [];
	queues['/api/v1/auth/setup/status'] = [];
	requests.length = 0;
}

// The studio guard reads the config and the account after a setup answer, so
// the mock answers as the server does once the install is decided.
function afterSetup(choice) {
	fixture.config =
		choice === 'accounts'
			? { status: 200, body: { ...noneConfig, auth_methods: ['password'] } }
			: { status: 200, body: noneConfig };
	fixture.account =
		choice === 'accounts'
			? {
					status: 200,
					body: {
						id: '0f0e4c1a-7b2d-4e8f-9a3c-5d6e7f8a9b0c',
						email: 'ana@example.com',
						role: 'admin'
					}
				}
			: { status: 404, body: { detail: 'Not Found' } };
}

function nextReply(method, path) {
	return queues[path]?.shift() ?? apiReply(method, path);
}

function apiReply(method, path) {
	if (path === '/api/v1/config') return fixture.config;
	if (path === '/api/v1/account') return fixture.account;
	if (path === '/api/v1/auth/setup') return fixture.setup;
	if (path === '/api/v1/auth/setup/status') return fixture.status;
	if (path === '/api/v1/auth/enable') return fixture.enable;
	if (method !== 'GET') return { status: 404, body: { detail: 'Not Found' } };
	if (path === '/api/v1/generations/models') return { status: 200, body: { models: [] } };
	return { status: 200, body: [] };
}

function readBody(request) {
	return new Promise((resolveBody) => {
		let text = '';
		request.on('data', (chunk) => (text += chunk));
		request.on('end', () => resolveBody(text));
	});
}

// The mocked API answers first; every other path is the build, with the app shell as fallback.
function serveSetupBuild() {
	return serveBuild(build, {
		fallback: 'index.html',
		api: async (request, response) => {
			const url = new URL(request.url, 'http://localhost');
			if (!url.pathname.startsWith('/api/')) return false;
			const text = await readBody(request);
			requests.push({
				method: request.method,
				path: url.pathname,
				body: text ? JSON.parse(text) : null
			});
			const answer = nextReply(request.method, url.pathname);
			if (answer.delayMs) {
				await pause(answer.delayMs);
				// Lets a test prove the answer was still out when its link changed.
				answer.answered = true;
			}
			if (answer.status === 204) {
				response.writeHead(204, { 'Cache-Control': 'no-store' }).end();
				return true;
			}
			response.writeHead(answer.status, {
				'Content-Type': 'application/json',
				'Cache-Control': 'no-store'
			});
			if (answer.bodyGate) {
				response.flushHeaders();
				answer.headersSent = true;
				await answer.bodyGate;
			}
			response.end(JSON.stringify(answer.body));
			answer.bodySent = true;
			return true;
		}
	});
}

async function open(browser, server, path) {
	const page = await newStudioPage(browser);
	page.setDefaultTimeout(timeoutMs);
	const errors = [];
	page.on('pageerror', (error) => {
		errors.push(error.stack ?? error.message);
		pageErrors.push(error.message);
	});
	const address = server.address();
	await page.goto(`http://127.0.0.1:${address.port}${path}`, { waitUntil: 'domcontentloaded' });
	return { page, errors };
}

// A background tab is hidden and gets no animation frames, so waitForFunction
// checks once and never again. Bring the tab to the front, as a user would.
async function waitOn(page, predicate, arg) {
	await page.bringToFront();
	await page.waitForFunction(predicate, {}, arg);
}

async function waitForText(page, text) {
	await waitOn(page, (needle) => document.body.innerText.includes(needle), text);
}

async function clickButton(page, label) {
	await waitOn(
		page,
		(selectedLabel) =>
			[...document.querySelectorAll('button')].some(
				(item) => item.textContent?.trim() === selectedLabel && !item.disabled
			),
		label
	);
	await page.evaluate((selectedLabel) => {
		const button = [...document.querySelectorAll('button')].find(
			(item) => item.textContent?.trim() === selectedLabel
		);
		button.click();
	}, label);
}

function postsTo(path) {
	return requests.filter((entry) => entry.method === 'POST' && entry.path === path);
}

async function waitForButton(page, label) {
	await waitOn(
		page,
		(selectedLabel) =>
			[...document.querySelectorAll('button')].some(
				(item) => item.textContent?.trim() === selectedLabel
			),
		label
	);
}

// Picks one of the two options on the choice. The option's fields appear after
// the pick, so the helper waits for them before a test types into them.
async function pickMode(page, value) {
	await waitOn(
		page,
		(selected) => document.querySelector(`input[name="setup-mode"][value="${selected}"]`) !== null,
		value
	);
	await page.click(`input[name="setup-mode"][value="${value}"]`);
	if (value === 'accounts') {
		await page.waitForSelector('#setup-email');
	} else {
		await waitForText(page, 'everyone who can reach this address is an administrator');
	}
}

async function checkedMode(page) {
	return page.evaluate(
		() => document.querySelector('input[name="setup-mode"]:checked')?.value ?? null
	);
}

// The studio is at /app once the setup answer has been accepted.
async function waitForStudio(page) {
	await waitOn(page, () => location.pathname === '/app');
}

async function waitUntil(predicate, description) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await pause(25);
	}
	throw new Error(`Timed out waiting for ${description}`);
}

async function inputCount(page) {
	return page.evaluate(() => document.querySelectorAll('input').length);
}

async function buttonDisabled(page, label) {
	return page.evaluate(
		(selectedLabel) =>
			[...document.querySelectorAll('button')].find(
				(item) => item.textContent?.trim() === selectedLabel
			)?.disabled ?? null,
		label
	);
}

async function testWithoutToken(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, '/setup');
	await waitForText(page, 'make setup-link');
	assert.match(
		await bodyText(page),
		/env -u COMPOSE_PROJECT_NAME docker compose --env-file deploy\/compose\/.env -f deploy\/compose\/compose.yml exec -T api python -m app.operator setup-link/
	);
	assert.equal(await page.$('#setup-email'), null, 'no form without a token');
	assert.equal(await inputCount(page), 0, 'no input without a token');
	assert.deepEqual(errors, []);
	await page.close();
}

async function testTokenReadAndAccountsChoice(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForText(page, 'Who will use this studio?');
	assert.deepEqual(postsTo('/api/v1/auth/setup/status').at(-1)?.body, { token });
	assert.equal(await page.evaluate(() => location.hash), '', 'token left the address bar');
	await pickMode(page, 'accounts');
	await page.type('#setup-email', 'ana@example.com');
	await page.type('#setup-password', password);
	await page.type('#setup-confirm', password);
	afterSetup('accounts');
	await clickButton(page, 'Create the administrator account');
	await waitForStudio(page);
	assert.deepEqual(postsTo('/api/v1/auth/setup').at(-1)?.body, {
		token,
		mode: 'accounts',
		email: 'ana@example.com',
		password
	});
	assert.deepEqual(errors, []);
	await page.close();
}

async function testPasswordControlsAndLength(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await pickMode(page, 'accounts');
		await page.type('#setup-email', 'ana@example.com');
		await page.type('#setup-password', 'Pepepepe1');
		await page.type('#setup-confirm', 'Pepepepe1');
		await page.focus('#setup-password');
		await page.keyboard.press('Tab');
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-label')),
			'Show password'
		);
		await page.keyboard.press('Space');
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-pressed')),
			'true'
		);
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-label')),
			'Show password'
		);
		await page.keyboard.press('Enter');
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-pressed')),
			'false'
		);
		assert.equal(await page.$eval('#setup-password', (input) => input.type), 'password');
		await page.focus('#setup-password');
		await page.click('button[aria-controls="setup-password"]');
		assert.equal(await page.$eval('#setup-password', (input) => input.type), 'text');
		assert.deepEqual(
			await page.$eval('#setup-password', (input) => [
				input.getAttribute('autocapitalize'),
				input.getAttribute('autocorrect'),
				input.spellcheck
			]),
			['none', 'off', false]
		);
		assert.equal(await page.$eval('#setup-password', (input) => input.value), 'Pepepepe1');
		assert.equal(await page.evaluate(() => document.activeElement?.id), 'setup-password');
		await page.click('button[aria-controls="setup-password"]');
		assert.equal(await page.$eval('#setup-password', (input) => input.type), 'password');
		await clickButton(page, 'Create the administrator account');
		await waitForText(page, 'This password has 9 characters. Use at least 10.');
		assert.equal(postsTo('/api/v1/auth/setup').length, 0);
		await page.type('#setup-password', '2');
		await page.type('#setup-confirm', '2');
		afterSetup('accounts');
		await clickButton(page, 'Create the administrator account');
		await waitForStudio(page);
		assert.equal(postsTo('/api/v1/auth/setup').length, 1);
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testPasswordPolicyRefusal(browser, server) {
	resetFixture();
	fixture.setup = { status: 400, body: { detail: 'password does not meet the policy' } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await pickMode(page, 'accounts');
		await page.type('#setup-email', 'ana@example.com');
		await page.type('#setup-password', password);
		await page.type('#setup-confirm', password);
		await clickButton(page, 'Create the administrator account');
		await waitForText(page, 'That password is too common. Pick a different one.');
		assert.equal(postsTo('/api/v1/auth/setup').length, 1);
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testOnlyMeNeedsItsSubmit(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForText(page, 'Who will use this studio?');
	await pickMode(page, 'none');
	assert.equal(postsTo('/api/v1/auth/setup').length, 0, 'nothing is sent before submit');
	afterSetup('none');
	await clickButton(page, 'Continue without accounts');
	await waitForStudio(page);
	assert.deepEqual(postsTo('/api/v1/auth/setup').at(-1)?.body, { token, mode: 'none' });
	assert.deepEqual(errors, []);
	await page.close();
}

async function testRefusedTokenShowsOnlyInstruction(browser, server) {
	resetFixture();
	fixture.setup = { status: 403, body: { detail: 'invalid or expired setup link' } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForText(page, 'Who will use this studio?');
	await pickMode(page, 'accounts');
	await page.type('#setup-email', 'ana@example.com');
	await page.type('#setup-password', password);
	await page.type('#setup-confirm', password);
	await clickButton(page, 'Create the administrator account');
	await waitForText(page, 'make setup-link');
	assert.equal(await page.$('#setup-email'), null, 'refused token hides the form');
	assert.equal(await inputCount(page), 0, 'refused token leaves no input');
	assert.equal(await page.evaluate(() => location.hash), '');
	assert.deepEqual(errors, []);
	await page.close();
}

async function testStatusRefusalAndLateBody(browser, server) {
	resetFixture();
	const slow = delayedBody(200, { claimed: true });
	queues['/api/v1/auth/setup/status'].push(slow.answer);
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitUntil(() => slow.answer.headersSent, 'status headers');
		await page.evaluate((next) => (location.hash = next), otherToken);
		await waitForButton(page, 'Continue');
		slow.release();
		await waitUntil(() => slow.answer.bodySent, 'status body');
		await pause(200);
		assert.notEqual(await buttonDisabled(page, 'Continue'), null);
		fixture.status = { status: 403, body: { detail: 'invalid or expired setup link' } };
		await page.evaluate((next) => (location.hash = next), token);
		await waitForText(page, 'This setup link is not valid');
		assert.equal(await inputCount(page), 0);
		assert.deepEqual(errors, []);
	} finally {
		slow.release();
		await page.close();
	}
}

async function testOverrideAccountsOffersClaimOnly(browser, server) {
	resetFixture();
	fixture.config = { status: 200, body: { ...noneConfig, auth_methods: ['password'] } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForText(page, 'Create the administrator account');
	assert.equal((await page.$('#setup-email')) !== null, true, 'the claim form is shown');
	assert.equal((await bodyText(page)).includes('Only me'), false);
	assert.equal(await buttonDisabled(page, 'Back'), null, 'no way back to the choice');
	await page.type('#setup-email', 'ana@example.com');
	await page.type('#setup-password', password);
	await page.type('#setup-confirm', password);
	afterSetup('accounts');
	await clickButton(page, 'Create the administrator account');
	await waitForStudio(page);
	assert.deepEqual(postsTo('/api/v1/auth/setup').at(-1)?.body, {
		token,
		mode: 'accounts',
		email: 'ana@example.com',
		password
	});
	assert.deepEqual(errors, []);
	await page.close();
}

async function testOverrideNoneSaysAlreadySetUp(browser, server) {
	resetFixture();
	fixture.config = { status: 200, body: noneConfig };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForText(page, 'This install is already set up.');
	assert.equal(await page.$('#setup-email'), null, 'no form');
	assert.equal((await page.$('a[href="/app"]')) !== null, true, 'a link to the studio');
	assert.equal(postsTo('/api/v1/auth/setup').length, 0, 'nothing is posted');
	assert.deepEqual(errors, []);
	await page.close();
}

async function testStaleTabAfterNoneChoice(browser, server) {
	resetFixture();
	const first = await open(browser, server, `/setup#${token}`);
	const second = await open(browser, server, `/setup#${token}`);
	try {
		await waitForButton(first.page, 'Continue');
		await waitForButton(second.page, 'Continue');
		await pickMode(first.page, 'none');
		// Answer the first tab late, so its wait starts before the result shows
		// while the second tab, opened last, is still in front.
		queues['/api/v1/auth/setup'].push({ status: 204, delayMs: 500 });
		afterSetup('none');
		await clickButton(first.page, 'Continue without accounts');
		await waitForStudio(first.page);
		fixture.config = { status: 200, body: noneConfig };
		fixture.setup = { status: 404, body: { detail: 'Not Found' } };
		await pickMode(second.page, 'none');
		await clickButton(second.page, 'Continue without accounts');
		await waitForText(second.page, 'This install is already set up.');
		assert.notEqual(await second.page.$('a[href="/app"]'), null);
		assert.deepEqual(first.errors, []);
		assert.deepEqual(second.errors, []);
	} finally {
		await first.page.close();
		await second.page.close();
	}
}

async function test404ReloadsAccountState(browser, server) {
	for (const claimed of [false, true]) {
		resetFixture();
		const { page, errors } = await open(browser, server, `/setup#${token}`);
		try {
			await pickMode(page, 'none');
			fixture.setup = { status: 404, body: { detail: 'Not Found' } };
			fixture.config = {
				status: 200,
				body: { ...noneConfig, auth_methods: ['password'] }
			};
			fixture.status = { status: 200, body: { claimed, expires_in: 3120 } };
			await clickButton(page, 'Continue without accounts');
			if (claimed) {
				await waitForText(page, 'This install is already set up.');
				assert.notEqual(await page.$('a[href="/app"]'), null);
			} else {
				await page.waitForSelector('#setup-email');
				assert.equal(await page.$('input[name="setup-mode"]'), null, 'the claim has no choice');
				assert.equal(await buttonDisabled(page, 'Back'), null);
			}
			assert.deepEqual(errors, []);
		} finally {
			await page.close();
		}
	}
}

async function bodyText(page) {
	return page.evaluate(() => document.body.innerText);
}

async function testLateRefusalDoesNotReplaceNewLink(browser, server) {
	resetFixture();
	const slow = {
		status: 403,
		body: { detail: 'invalid or expired setup link' },
		delayMs: 2500
	};
	queues['/api/v1/auth/setup'].push(slow);
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForButton(page, 'Continue');
	await pickMode(page, 'none');
	await clickButton(page, 'Continue without accounts');
	await waitUntil(() => postsTo('/api/v1/auth/setup').length === 1, 'the choice to post');
	await page.evaluate((next) => {
		location.hash = next;
	}, otherToken);
	await waitForButton(page, 'Continue');
	assert.equal(slow.answered, undefined, 'the refusal is still out');
	await waitUntil(() => slow.answered === true, 'the refusal to arrive');
	await pause(200);
	assert.equal(await page.$('#setup-email'), null);
	assert.equal(
		(await bodyText(page)).includes('make setup-link'),
		false,
		'the late refusal for the first link does not replace the second link'
	);
	assert.notEqual(await buttonDisabled(page, 'Continue'), null, 'the choice stays');
	assert.equal(postsTo('/api/v1/auth/setup').length, 1);
	assert.deepEqual(errors, []);
	await page.close();
}

function delayedBody(status, body) {
	let release;
	const bodyGate = new Promise((resolveBody) => (release = resolveBody));
	return { answer: { status, body, bodyGate }, release };
}

async function testLate404ConfigDoesNotReplaceNewLink(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	const slow = delayedBody(200, noneConfig);
	try {
		await pickMode(page, 'none');
		fixture.setup = { status: 404, body: { detail: 'Not Found' } };
		queues['/api/v1/config'].push(slow.answer);
		await clickButton(page, 'Continue without accounts');
		await waitUntil(() => slow.answer.headersSent, 'reloaded config headers');
		await page.evaluate((next) => (location.hash = next), otherToken);
		await waitForButton(page, 'Continue');
		slow.release();
		await waitUntil(() => slow.answer.bodySent, 'reloaded config body');
		await pause(200);
		assert.notEqual(await buttonDisabled(page, 'Continue'), null);
		assert.deepEqual(errors, []);
	} finally {
		slow.release();
		await page.close();
	}
}

async function testLateClaimBodyDoesNotAffectNewLink(browser, server) {
	resetFixture();
	const slow = delayedBody(400, { detail: 'invalid email' });
	queues['/api/v1/auth/setup'].push(slow.answer);
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await pickMode(page, 'none');
		await clickButton(page, 'Continue without accounts');
		await waitUntil(() => slow.answer.headersSent, 'claim headers');
		await page.evaluate((next) => (location.hash = next), otherToken);
		await waitForButton(page, 'Continue');
		await pickMode(page, 'accounts');
		assert.equal(await page.$('[role="alert"]'), null);
		slow.release();
		await waitUntil(() => slow.answer.bodySent, 'claim body');
		await pause(200);
		assert.equal(await page.$('[role="alert"]'), null, 'old claim error must not affect new link');
		assert.notEqual(await page.$('#setup-email'), null);
		assert.deepEqual(errors, []);
	} finally {
		slow.release();
		await page.close();
	}
}

async function testLateConfigBodyDoesNotReplaceNewLink(browser, server) {
	resetFixture();
	const slow = delayedBody(200, unclaimedConfig);
	queues['/api/v1/config'].push(slow.answer, { status: 200, body: noneConfig });
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitUntil(() => slow.answer.headersSent, 'config headers');
		await page.evaluate((next) => (location.hash = next), otherToken);
		await waitForText(page, 'This install is already set up.');
		assert.equal(slow.answer.bodySent, undefined);
		slow.release();
		await waitUntil(() => slow.answer.bodySent, 'config body');
		await pause(200);
		assert.match(await bodyText(page), /This install is already set up\./);
		assert.equal(await buttonDisabled(page, 'Continue'), null);
		assert.deepEqual(errors, []);
	} finally {
		slow.release();
		await page.close();
	}
}

async function testChoiceSendsOnce(browser, server) {
	resetFixture();
	queues['/api/v1/auth/setup'].push({ status: 204, delayMs: 600 });
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForButton(page, 'Continue');
	await pickMode(page, 'none');
	await waitForButton(page, 'Continue without accounts');
	afterSetup('none');
	await page.evaluate(() => {
		const button = [...document.querySelectorAll('button')].find(
			(item) => item.textContent?.trim() === 'Continue without accounts'
		);
		button.click();
		button.click();
	});
	await waitForStudio(page);
	await pause(300);
	assert.equal(postsTo('/api/v1/auth/setup').length, 1, 'two clicks post once');
	assert.deepEqual(errors, []);
	await page.close();
}

async function testConfigFailureRetriesWithTheSameLink(browser, server) {
	resetFixture();
	queues['/api/v1/config'].push({ status: 503, body: { detail: 'database unavailable' } });
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	await waitForButton(page, 'Try again');
	assert.equal(await page.evaluate(() => location.hash), '', 'the link left the address bar');
	assert.equal((await bodyText(page)).includes('make setup-link'), false);
	assert.equal(await page.$('#setup-email'), null);
	await clickButton(page, 'Try again');
	await waitForButton(page, 'Continue');
	await pickMode(page, 'none');
	assert.equal(requests.filter((entry) => entry.path === '/api/v1/config').length, 2);
	afterSetup('none');
	await clickButton(page, 'Continue without accounts');
	await waitForStudio(page);
	assert.deepEqual(postsTo('/api/v1/auth/setup').at(-1)?.body, { token, mode: 'none' });
	assert.deepEqual(errors, []);
	await page.close();
}

async function testAppSendsUnclaimedToSetup(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, '/app');
	await page.waitForFunction(() => location.pathname === '/setup');
	await waitForText(page, 'make setup-link');
	assert.ok(
		requests.some((entry) => entry.path === '/api/v1/config'),
		'the guard read the config'
	);
	assert.deepEqual(errors, []);
	await page.close();
}

async function testNoneSettingsTurnOnAccounts(browser, server) {
	resetFixture();
	fixture.config = { status: 200, body: noneConfig };
	fixture.account = { status: 404, body: { detail: 'Not Found' } };
	const { page, errors } = await open(browser, server, '/app?view=account');
	await page.waitForSelector('#turn-on-email');
	await page.waitForFunction(() => !document.activeViewTransition, { timeout: 2000 });
	await page.type('#turn-on-email', 'ana@example.com');
	await page.type('#turn-on-password', password);
	assert.equal(await buttonDisabled(page, 'Turn on accounts'), true, 'confirm gates the submit');
	await page.click('#turn-on-confirm');
	assert.equal(await buttonDisabled(page, 'Turn on accounts'), false);
	await clickButton(page, 'Turn on accounts');
	await page.waitForFunction(
		() =>
			location.pathname === '/app' &&
			location.search === '' &&
			!document.querySelector('#turn-on-email')
	);
	assert.deepEqual(postsTo('/api/v1/auth/enable').at(-1)?.body, {
		email: 'ana@example.com',
		password
	});
	assert.deepEqual(errors, []);
	await page.close();
}

async function testNoOptionIsPreselected(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitForText(page, 'Who will use this studio?');
		assert.equal(await checkedMode(page), null, 'no option is preselected');
		assert.equal(await buttonDisabled(page, 'Continue'), true, 'submit waits for a choice');
		await pickMode(page, 'none');
		assert.equal(await buttonDisabled(page, 'Continue without accounts'), false);
		assert.equal(postsTo('/api/v1/auth/setup').length, 0);
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testExpiryShowsMinutesLeft(browser, server) {
	resetFixture();
	fixture.status = { status: 200, body: { claimed: false, expires_in: 3120 } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitForText(page, 'It expires in 52 min.');
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testEnterInConfirmSubmitsAccounts(browser, server) {
	resetFixture();
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitForText(page, 'Who will use this studio?');
		await pickMode(page, 'accounts');
		await page.type('#setup-email', 'ana@example.com');
		await page.type('#setup-password', password);
		await page.type('#setup-confirm', password);
		afterSetup('accounts');
		await page.focus('#setup-confirm');
		await page.keyboard.press('Enter');
		await waitForStudio(page);
		assert.deepEqual(postsTo('/api/v1/auth/setup').at(-1)?.body, {
			token,
			mode: 'accounts',
			email: 'ana@example.com',
			password
		});
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testClaimCaseHasNoOnlyMeOption(browser, server) {
	resetFixture();
	fixture.config = { status: 200, body: { ...noneConfig, auth_methods: ['password'] } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await page.waitForSelector('#setup-email');
		assert.equal(await page.$('input[name="setup-mode"]'), null, 'no radio options');
		assert.equal((await bodyText(page)).includes('Only me'), false);
		assert.equal(
			await page.$eval('h1', (heading) => heading.textContent?.trim()),
			'Create the administrator account'
		);
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testOlderApiShowsNoExpiry(browser, server) {
	resetFixture();
	fixture.status = { status: 200, body: { claimed: false } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitForText(page, 'Who will use this studio?');
		assert.equal((await bodyText(page)).includes('expires in'), false);
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testOriginRefusalKeepsToken(browser, server) {
	resetFixture();
	queues['/api/v1/auth/setup'].push(
		{ status: 403, body: { detail: 'origin not allowed' } },
		{ status: 204 }
	);
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await pickMode(page, 'accounts');
		await page.type('#setup-email', 'ana@example.com');
		await page.type('#setup-password', password);
		await page.type('#setup-confirm', password);
		await clickButton(page, 'Create the administrator account');
		await waitForText(page, messages['auth.error.origin_not_allowed']);
		assert.notEqual(await page.$('#setup-email'), null, 'origin refusal keeps the form');
		afterSetup('accounts');
		await clickButton(page, 'Create the administrator account');
		await waitForStudio(page);
		const posts = postsTo('/api/v1/auth/setup');
		assert.equal(posts.length, 2);
		assert.equal(posts[0]?.body?.token, token);
		assert.equal(posts[1]?.body?.token, token);
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function testZeroExpiryShowsInstruction(browser, server) {
	resetFixture();
	fixture.status = { status: 200, body: { claimed: false, expires_in: 0 } };
	const { page, errors } = await open(browser, server, `/setup#${token}`);
	try {
		await waitForText(page, 'This setup link is not valid');
		assert.equal(await page.$('#setup-email'), null);
		assert.equal(await inputCount(page), 0, 'an expired link leaves no input');
		assert.deepEqual(errors, []);
	} finally {
		await page.close();
	}
}

async function main() {
	const { server } = await serveSetupBuild();
	let browser;
	const failures = [];
	const cases = [
		['without a token the page shows the instruction and no form', testWithoutToken],
		[
			'the token leaves the address bar and the accounts choice posts it',
			testTokenReadAndAccountsChoice
		],
		['password visibility and length are checked before submitting', testPasswordControlsAndLength],
		['a password policy refusal explains the blocklist', testPasswordPolicyRefusal],
		['the only-me choice posts only from its submit button', testOnlyMeNeedsItsSubmit],
		['a refused token shows only the instruction', testRefusedTokenShowsOnlyInstruction],
		['status refusal and late status body respect the link', testStatusRefusalAndLateBody],
		[
			'an override that decided accounts offers the claim alone',
			testOverrideAccountsOffersClaimOnly
		],
		[
			'an override that decided none says the install is already set up',
			testOverrideNoneSaysAlreadySetUp
		],
		['a stale tab reloads config after a none choice', testStaleTabAfterNoneChoice],
		['a 404 reloads the accounts claim state', test404ReloadsAccountState],
		['a late 404 config does not replace a new link', testLate404ConfigDoesNotReplaceNewLink],
		[
			'a late refusal for an earlier link does not replace a new one',
			testLateRefusalDoesNotReplaceNewLink
		],
		['a late claim body does not affect a new link', testLateClaimBodyDoesNotAffectNewLink],
		['a late config body does not replace a new link', testLateConfigBodyDoesNotReplaceNewLink],
		['a double click on a choice posts once', testChoiceSendsOnce],
		['a failed config read is retried with the same link', testConfigFailureRetriesWithTheSameLink],
		['the studio sends an unclaimed install to setup', testAppSendsUnclaimedToSetup],
		['settings turns accounts on in none mode', testNoneSettingsTurnOnAccounts],
		['no option is preselected and submit waits for a choice', testNoOptionIsPreselected],
		['the status answer shows the minutes left on the link', testExpiryShowsMinutesLeft],
		[
			'Enter in the password confirmation submits the accounts form',
			testEnterInConfirmSubmitsAccounts
		],
		['the claim case shows no only-me option', testClaimCaseHasNoOnlyMeOption],
		['an older API without expires_in shows no expiry line', testOlderApiShowsNoExpiry],
		['an expired link shows the instruction', testZeroExpiryShowsInstruction],
		[
			'an origin refusal keeps the token and a later submit can succeed',
			testOriginRefusalKeepsToken
		]
	];
	try {
		browser = await launchChrome({ args: ['--disable-dev-shm-usage'] });
		for (const [name, run] of cases) {
			try {
				await run(browser, server);
				pageErrors.length = 0;
				console.log(`ok - ${name}`);
			} catch (error) {
				failures.push(name);
				console.error(`not ok - ${name}\n${error.stack ?? error}`);
				for (const message of pageErrors.splice(0)) console.error(`  page error: ${message}`);
			}
		}
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
	if (failures.length > 0) {
		throw new Error(`${failures.length} setup page check(s) failed`);
	}
}

await main();
