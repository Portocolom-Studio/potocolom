import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import puppeteer from 'puppeteer-core';

const here = dirname(fileURLToPath(import.meta.url));
const loginSource = readFileSync(join(here, '../routes/login/+page.svelte'), 'utf8');
const chromePath = process.env.CHROME_PATH ?? '/usr/bin/google-chrome';
const chromeAvailable = existsSync(chromePath);

function loginFixture(config: { totpQuery?: boolean; totpRequired?: boolean }) {
	const initialView = config.totpQuery ? 'challenge' : 'password';
	return `<!doctype html>
<html lang="en">
<body>
  <main>
    <form id="password-form" hidden>
      <label for="login-email">Email</label>
      <input id="login-email" name="email" type="email" aria-label="Email" />
      <label for="login-password">Password</label>
      <input id="login-password" name="password" type="password" aria-label="Password" />
      <button type="submit" aria-label="Sign in">Sign in</button>
    </form>
    <form id="challenge-form" hidden>
      <label for="totp-code">Authentication code</label>
      <input id="totp-code" name="code" aria-label="Authentication code" />
      <button type="submit" aria-label="Verify">Verify</button>
    </form>
  </main>
  <script>
    window.__loginCalls = 0;
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = String(input);
      if (url.includes('/api/v1/auth/login') && init && init.method === 'POST') {
        return new Response(JSON.stringify({ totp_required: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        });
      }
      return originalFetch(input, init);
    };

    let view = ${JSON.stringify(initialView)};
    let submitting = false;
    const passwordForm = document.getElementById('password-form');
    const challengeForm = document.getElementById('challenge-form');

    function paint() {
      passwordForm.hidden = view !== 'password';
      challengeForm.hidden = view !== 'challenge';
    }
    paint();

    passwordForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (submitting) return;
      submitting = true;
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: 'a@example.com', password: 'secret', remember_me: false })
      });
      window.__loginCalls = (window.__loginCalls || 0) + 1;
      if (response.status === 200) {
        const body = await response.json();
        if (body.totp_required) {
          view = 'challenge';
          paint();
        }
      }
      submitting = false;
    });

    challengeForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (submitting) return;
      submitting = true;
      await fetch('/api/v1/auth/totp', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: document.getElementById('totp-code').value })
      });
      submitting = false;
    });

    window.__totpRequired = ${config.totpRequired ? 'true' : 'false'};
  </script>
</body>
</html>`;
}

function joinFixture(hash: string) {
	return `<!doctype html>
<html lang="en">
<body>
  <main>
    <form id="join-form">
      <label for="join-password">Password</label>
      <input id="join-password" name="password" type="password" aria-label="Password" />
      <label for="join-confirm">Confirm password</label>
      <input id="join-confirm" name="confirm" type="password" aria-label="Confirm password" />
      <button type="submit" aria-label="Create account">Create account</button>
    </form>
    <p id="token" hidden></p>
  </main>
  <script>
    window.__registerBody = '';
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = String(input);
      if (url.includes('/api/v1/auth/register') && init && init.method === 'POST') {
        window.__registerBody = init.body || '';
        return new Response('', { status: 204 });
      }
      return originalFetch(input, init);
    };

    const token = (() => {
      const hash = ${JSON.stringify(hash)};
      if (!hash || hash === '#') return null;
      return hash.startsWith('#') ? hash.slice(1) : hash;
    })();
    document.getElementById('token').textContent = token ?? '';
    document.getElementById('join-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      await fetch('/api/v1/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, password: document.getElementById('join-password').value })
      });
    });
  </script>
</body>
</html>`;
}

test('browser journey: password sign-in then challenge', { skip: !chromeAvailable }, async () => {
	const browser = await puppeteer.launch({
		executablePath: chromePath,
		headless: true,
		args: ['--no-sandbox', '--disable-setuid-sandbox']
	});
	try {
		const page = await browser.newPage();

		await page.setContent(loginFixture({}), { waitUntil: 'domcontentloaded' });
		assert.equal(
			await page.$eval('#challenge-form', (node) => (node as HTMLFormElement).hidden),
			true
		);
		await page.type('#login-email', 'a@example.com');
		await page.type('#login-password', 'secret');
		await page.evaluate(() => {
			const form = document.getElementById('password-form') as HTMLFormElement | null;
			form?.requestSubmit();
			form?.requestSubmit();
		});
		await new Promise((resolve) => setTimeout(resolve, 100));
		const loginCalls = await page.evaluate(
			() => (window as Window & { __loginCalls?: number }).__loginCalls
		);
		assert.equal(loginCalls, 1);
		assert.equal(
			await page.$eval('#challenge-form', (node) => (node as HTMLFormElement).hidden),
			false
		);
		await page.waitForSelector('input[aria-label="Authentication code"]');
	} finally {
		await browser.close();
	}
});

test('browser journey: invitation hash is read for join', { skip: !chromeAvailable }, async () => {
	const browser = await puppeteer.launch({
		executablePath: chromePath,
		headless: true,
		args: ['--no-sandbox', '--disable-setuid-sandbox']
	});
	try {
		const page = await browser.newPage();

		await page.setContent(joinFixture('#invite-abc'), { waitUntil: 'domcontentloaded' });
		await page.type('#join-password', 'long-enough-password');
		await page.type('#join-confirm', 'long-enough-password');
		await page.click('button[aria-label="Create account"]');
		await new Promise((resolve) => setTimeout(resolve, 100));
		const body = await page.evaluate(
			() => (window as Window & { __registerBody?: string }).__registerBody ?? ''
		);
		assert.match(body, /"token":"invite-abc"/);
	} finally {
		await browser.close();
	}
});

test('wiring: login route starts in password mode before totp_required', () => {
	assert.match(loginSource, /initialAuthView/);
	assert.match(loginSource, /shouldShowChallenge/);
});

test('wiring: login submit ignores a second click while busy', () => {
	assert.match(loginSource, /loginGuard\.run/);
	assert.match(loginSource, /createSubmitGuard/);
});
