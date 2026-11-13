import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { readFile, stat, access } from 'node:fs/promises';
import { resolve, join, extname } from 'node:path';
import puppeteer from 'puppeteer-core';

// The fleet tab against a mocked API: list, pause, resume, add with the
// shown-once command, connection, removal, and the address rules. The studio
// is served from the product build (frontend/build). A .lan name is mapped to
// the loopback server, so the dialog sees a private plain-http address.

const root = resolve('build');
const hour = 3600_000;
const iso = (ms) => new Date(ms).toISOString();
const types = {
	'.html': 'text/html',
	'.js': 'application/javascript',
	'.css': 'text/css',
	'.json': 'application/json',
	'.woff2': 'font/woff2',
	'.svg': 'image/svg+xml'
};

// The mocked API's join token for the nth member created.
function issuedToken(n) {
	return `tok-scenario-${n}`;
}

function seedMembers(now) {
	return [
		{
			id: 'm-a',
			worker_id: 'm-a1',
			label: 'Desk GPU',
			device: 'cuda',
			state: 'connected',
			paused: false,
			created_at: iso(now - 2 * hour),
			enrolled_at: iso(now - 2 * hour),
			last_seen_at: iso(now),
			join_expires_at: null
		},
		{
			id: 'm-b',
			worker_id: 'm-b1',
			label: 'Spare AMD',
			device: 'rocm',
			state: 'offline',
			paused: true,
			created_at: iso(now - 5 * hour),
			enrolled_at: iso(now - 5 * hour),
			last_seen_at: iso(now - hour),
			join_expires_at: null
		},
		{
			id: 'm-r',
			worker_id: 'm-r1',
			label: 'Old box',
			device: 'cuda',
			state: 'revoked',
			paused: false,
			created_at: iso(now - 9 * hour),
			enrolled_at: iso(now - 9 * hour),
			last_seen_at: null,
			join_expires_at: null
		}
	];
}

let members = [];
let created = 0;
let createDenials = 0;
let patchFailures = 0;
let calls = [];
let patches = [];
let deletes = [];

async function readBody(req) {
	const chunks = [];
	for await (const chunk of req) chunks.push(chunk);
	return Buffer.concat(chunks).toString('utf8');
}

test('fleet tab lists members, pauses, adds a GPU, connects and removes it', async () => {
	members = seedMembers(Date.now());
	const server = createServer(async (req, res) => {
		const path = new URL(req.url, 'http://localhost').pathname;
		if (path.startsWith('/api/')) {
			const raw = await readBody(req);
			calls.push(`${req.method} ${path}`);
			const json = (status, body) =>
				res.writeHead(status, { 'Content-Type': 'application/json' }).end(JSON.stringify(body));
			if (path === '/api/v1/account')
				return json(200, {
					id: '11000000-0000-4000-8000-000000000001',
					email: 'admin@example.test',
					role: 'admin'
				});
			if (path === '/api/v1/config')
				return json(200, { auth_methods: ['password'], billing_enabled: false });
			if (path === '/api/v1/metrics/gpu/history')
				return json(200, { from: '', to: '', rollup: 'raw', samples: [] });
			if (path === '/api/v1/admin/models')
				return json(200, [{ worker_id: 'm-a1' }, { worker_id: 'builtin-1' }]);
			if (path === '/api/v1/admin/fleet/members' && req.method === 'GET') return json(200, members);
			if (path === '/api/v1/admin/fleet/members' && req.method === 'POST') {
				if (createDenials > 0) {
					createDenials--;
					return json(403, { detail: 'recent authentication required' });
				}
				const body = JSON.parse(raw);
				const now = Date.now();
				created++;
				const member = {
					id: `m-new-${created}`,
					worker_id: `m-new-${created}1`,
					label: body.label,
					device: body.device,
					state: 'waiting',
					paused: false,
					created_at: iso(now),
					enrolled_at: null,
					last_seen_at: null,
					join_expires_at: iso(now + 15 * 60_000)
				};
				members.push(member);
				return json(201, {
					id: member.id,
					label: member.label,
					device: member.device,
					join_token: issuedToken(created),
					expires_at: member.join_expires_at,
					worker_image: `ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-${body.device === 'cuda' ? 'cuda' : 'rocm'}`
				});
			}
			const match = path.match(/^\/api\/v1\/admin\/fleet\/members\/([^/]+)$/);
			if (match) {
				const member = members.find((entry) => entry.id === decodeURIComponent(match[1]));
				if (req.method === 'PATCH') {
					const body = JSON.parse(raw);
					patches.push(body);
					if (member === undefined || member.state === 'revoked')
						return json(409, { detail: 'member was removed' });
					if (patchFailures > 0) {
						patchFailures--;
						return json(409, { detail: 'fleet busy' });
					}
					member.paused = body.paused;
					return res.writeHead(204).end();
				}
				if (req.method === 'DELETE') {
					deletes.push(match[1]);
					if (member) member.state = 'revoked';
					return res.writeHead(204).end();
				}
			}
			return json(200, []);
		}
		const file = resolve(root, `.${decodeURIComponent(path)}`);
		if (!file.startsWith(root)) {
			res.writeHead(404).end();
			return;
		}
		for (const candidate of [
			file,
			`${file}.html`,
			join(file, 'index.html'),
			join(root, '200.html')
		]) {
			if (
				await stat(candidate).then(
					(s) => s.isFile(),
					() => false
				)
			) {
				res
					.writeHead(200, {
						'Content-Type': types[extname(candidate)] ?? 'application/octet-stream'
					})
					.end(await readFile(candidate));
				return;
			}
		}
		res.writeHead(404).end();
	});
	await new Promise((done) => server.listen(0, '127.0.0.1', done));
	const port = server.address().port;
	const localBase = `http://127.0.0.1:${port}`;
	const lanBase = `http://studio.lan:${port}`;
	const count = (needle) => calls.filter((call) => call === needle).length;
	let browser;
	try {
		const candidates = [
			process.env.PUPPETEER_EXECUTABLE_PATH,
			process.env.CHROME_PATH,
			process.env.CHROME_BIN,
			'/usr/bin/google-chrome',
			'/usr/bin/chromium',
			'/usr/bin/chromium-browser'
		].filter(Boolean);
		let executablePath;
		for (const candidate of candidates)
			if (
				await access(candidate).then(
					() => true,
					() => false
				)
			) {
				executablePath = candidate;
				break;
			}
		assert.ok(executablePath, 'Chrome is required');
		browser = await puppeteer.launch({
			executablePath,
			headless: true,
			args: [
				'--no-sandbox',
				'--disable-setuid-sandbox',
				'--host-resolver-rules=MAP studio.lan 127.0.0.1'
			]
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 900 });
		// This scenario opens the fleet tab as an administrator's first visit. The
		// first-run GPU card would cover the Add button, so it is marked done here.
		await page.evaluateOnNewDocument(() => {
			try {
				localStorage.setItem('potocolom-gpu-card-done', '1');
			} catch {
				// Blocked storage: the card does not show there either.
			}
		});
		const bodyText = () => page.evaluate(() => document.body.textContent);
		const waitForText = (needle) =>
			page.waitForFunction(
				(n) => document.body.textContent.includes(n),
				{ timeout: 15000 },
				needle
			);
		const waitUntil = async (predicate, timeout = 10000) => {
			const deadline = Date.now() + timeout;
			while (!predicate()) {
				assert.ok(Date.now() < deadline, 'condition not reached in time');
				await new Promise((done) => setTimeout(done, 100));
			}
		};
		const dialogGone = () =>
			page.waitForSelector('[role="dialog"]', { hidden: true, timeout: 5000 });

		// 1. On localhost the dialog asks for the address other machines use.
		await page.goto(`${localBase}/app?view=admin&tab=fleet`, { waitUntil: 'domcontentloaded' });
		await page.waitForSelector('[data-testid="admin-fleet-members"]');
		await page.click('[data-testid="admin-fleet-add"]');
		await page.waitForSelector('[data-testid="admin-fleet-lan"]');
		assert.match(await bodyText(), /localhost/);
		await page.type('[data-testid="admin-fleet-name"]', 'Lab GPU');
		assert.equal(
			await page.$eval('[data-testid="admin-fleet-create"]', (button) => button.disabled),
			true
		);
		await page.type('[data-testid="admin-fleet-lan"]', 'http://8.8.8.8');
		await waitForText('A public address needs https');
		assert.equal(
			await page.$eval('[data-testid="admin-fleet-create"]', (button) => button.disabled),
			true
		);
		await page.focus('[data-testid="admin-fleet-lan"]');
		await page.$eval('[data-testid="admin-fleet-lan"]', (field) => field.select());
		await page.keyboard.press('Backspace');
		await page.waitForFunction(
			() => document.querySelector('[data-testid="admin-fleet-lan"]').value === ''
		);
		await page.type('[data-testid="admin-fleet-lan"]', lanBase);
		await page.waitForSelector('[data-testid="admin-fleet-insecure"]');
		await page.waitForFunction(
			() => !document.querySelector('[data-testid="admin-fleet-create"]').disabled
		);

		// The first submit is refused for a recent sign-in: the hint shows and the
		// dialog stays open; the second submit succeeds.
		createDenials = 1;
		await page.select('[data-testid="admin-fleet-device"]', 'rocm');
		await page.click('[data-testid="admin-fleet-create"]');
		await page.waitForSelector('[data-testid="admin-fleet-recent-auth-hint"]');
		await page.click('[data-testid="admin-fleet-create"]');
		await page.waitForSelector('[data-testid="admin-fleet-command"]');
		const command = await page.$eval('[data-testid="admin-fleet-command"]', (field) => field.value);
		assert.match(command, /docker run -d --name potocolom-worker --restart unless-stopped/);
		assert.match(command, /--device \/dev\/kfd \\/);
		assert.match(command, /-e DEVICE=rocm \\/);
		assert.ok(command.includes(`-e API_URL=ws://studio.lan:${port}/api/v1/fleet \\`));
		assert.ok(command.includes(`-e JOIN_TOKEN=${issuedToken(1)} \\`));
		assert.ok(command.includes('-e ALLOW_INSECURE_FLEET=true \\'));
		assert.ok(command.endsWith('ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-rocm'));
		await page.waitForFunction(() =>
			/\d+:\d{2}/.test(document.querySelector('[data-testid="admin-fleet-status"]').textContent)
		);
		assert.match(
			await page.$eval('[data-testid="admin-fleet-status"]', (el) => el.textContent),
			/Waiting for the machine/
		);
		assert.equal(
			await page.$eval('[data-testid="admin-fleet-status"]', (el) =>
				el.closest('[aria-live]')?.getAttribute('aria-live')
			),
			'polite'
		);
		await page.click('[data-testid="admin-fleet-close"]');
		await dialogGone();
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('data-testid')),
			'admin-fleet-add',
			'focus returns to the Add button'
		);

		// 2. On the private name the page origin is used directly and the list holds
		// the members, the built-in worker and no revoked row.
		await page.goto(`${lanBase}/app?view=admin&tab=fleet`, { waitUntil: 'domcontentloaded' });
		await page.waitForSelector('[data-testid="admin-fleet-members"]');
		await waitForText('Lab GPU');
		const rowText = () =>
			page.$$eval('[data-testid="admin-fleet-member"]', (rows) =>
				rows.map((row) => row.textContent)
			);
		const rows = await rowText();
		assert.equal(rows.length, 3);
		assert.ok(rows.some((row) => row.includes('Desk GPU')));
		assert.ok(rows.some((row) => row.includes('Spare AMD') && row.includes('Paused')));
		assert.ok(rows.some((row) => row.includes('Waiting for the machine')));
		assert.equal(
			rows.some((row) => row.includes('Old box')),
			false
		);
		assert.equal(await page.$$eval('[data-testid="admin-fleet-builtin"]', (r) => r.length), 1);
		assert.match(await bodyText(), /builtin-1/);

		// 3. Pause and resume, with the keyboard, then a failed action on the row.
		await page.focus('button[aria-label="Pause: Desk GPU"]');
		await page.keyboard.press('Enter');
		await page.waitForSelector('button[aria-label="Resume: Desk GPU"]');
		assert.deepEqual(patches.at(-1), { paused: true });
		await page.click('button[aria-label="Resume: Desk GPU"]');
		await page.waitForSelector('button[aria-label="Pause: Desk GPU"]');
		assert.deepEqual(patches.at(-1), { paused: false });
		patchFailures = 1;
		await page.click('button[aria-label="Pause: Desk GPU"]');
		await page.waitForFunction(() =>
			[...document.querySelectorAll('[data-testid="admin-fleet-member"]')].some((row) =>
				row.querySelector('[role="alert"]')?.textContent.includes('fleet busy')
			)
		);
		assert.ok(await page.$('button[aria-label="Pause: Desk GPU"]'), 'the row is kept');

		// 4. Adding a second GPU shows the connection change while the dialog is open.
		await page.click('[data-testid="admin-fleet-add"]');
		await page.waitForSelector('[data-testid="admin-fleet-name"]');
		await page.type('[data-testid="admin-fleet-name"]', 'Second GPU');
		await page.waitForFunction(
			() => !document.querySelector('[data-testid="admin-fleet-create"]').disabled
		);
		await page.click('[data-testid="admin-fleet-create"]');
		await page.waitForSelector('[data-testid="admin-fleet-command"]');
		assert.ok(
			(await page.$eval('[data-testid="admin-fleet-command"]', (field) => field.value)).includes(
				`JOIN_TOKEN=${issuedToken(2)} \\`
			)
		);
		await page.click('[data-testid="admin-fleet-copy"]');
		await page.waitForFunction(() => {
			const text = document.body.textContent;
			return text.includes('Copied.') || text.includes('Could not copy automatically');
		});
		// While the member waits, the dialog polls about once a second.
		const polls = count('GET /api/v1/admin/fleet/members');
		await waitUntil(() => count('GET /api/v1/admin/fleet/members') >= polls + 2, 6000);
		const secondId = members.find((member) => member.label === 'Second GPU').id;
		members.find((member) => member.id === secondId).state = 'connected';
		await page.waitForFunction(() =>
			document.querySelector('[data-testid="admin-fleet-status"]').textContent.includes('Connected')
		);
		await page.click('[data-testid="admin-fleet-close"]');
		await dialogGone();
		await waitForText('Second GPU');

		// 5. Removal asks first. Cancel keeps the row; confirm removes it.
		await page.locator('button[aria-label="Remove: Spare AMD"]').click();
		await page.waitForSelector('[data-testid="admin-fleet-remove-confirm"]');
		assert.match(await bodyText(), /Work on that machine stops/);
		await page.keyboard.press('Escape');
		await dialogGone();
		assert.equal(deletes.length, 0);
		assert.equal(
			await page.evaluate(() => document.activeElement?.getAttribute('aria-label')),
			'Remove: Spare AMD',
			'a cancelled removal returns focus to the row'
		);
		assert.ok(await page.$('button[aria-label="Remove: Spare AMD"]'));
		await page.locator('button[aria-label="Remove: Spare AMD"]').click();
		await page.waitForSelector('[data-testid="admin-fleet-remove-confirm"]');
		await page.click('[data-testid="admin-fleet-remove-confirm"]');
		await page.waitForFunction(
			() =>
				![...document.querySelectorAll('[data-testid="admin-fleet-member"]')].some((row) =>
					row.textContent.includes('Spare AMD')
				)
		);
		assert.deepEqual(deletes, ['m-b']);
		await page.waitForFunction(
			() => document.activeElement?.getAttribute('data-testid') === 'admin-fleet-add'
		);
	} finally {
		if (browser) await browser.close();
		await new Promise((done) => server.close(done));
	}
});
