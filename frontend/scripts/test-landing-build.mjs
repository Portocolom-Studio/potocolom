import assert from 'node:assert/strict';
import { constants } from 'node:fs';
import { access, readFile, stat } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, join, resolve, sep } from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer-core';

test('the built landing /app contains the studio preview', async () => {
	const html = await readFile(new URL('../build/app.html', import.meta.url), 'utf8');
	const body = html.slice(html.indexOf('<body'), html.indexOf('</body>'));
	assert.match(body, /<div\s[^>]*\bdata-studio-preview(?:\s|[=>])/);
});

const built = (name) => readFile(new URL(`../build/${name}.html`, import.meta.url), 'utf8');

test('the landing build prerenders the public-site pages', async () => {
	for (const name of [
		'whitepaper',
		'benchmark',
		'illusions',
		'legal',
		'privacy',
		'hero-preview',
		'collage-preview',
		'docs',
		'docs/guide/introduction'
	]) {
		assert.match(await built(name), /<html\b/, name);
	}
});

test('the landing build prerenders every collage variant document', async () => {
	const source = await readFile(new URL('../src/lib/collage-variants.ts', import.meta.url), 'utf8');
	const variants = [...source.matchAll(/^\t(?:'([^']+)'|(\w[\w-]*)): \{/gm)].map(
		(match) => match[1] ?? match[2]
	);
	assert.ok(variants.includes('masonry') && variants.includes('graph'), 'variant ids');
	for (const id of variants) {
		assert.match(await built(`collage-preview/${id}`), /<html\b/, id);
	}
});

test('built pages share one horizon footer with the same landmarks', async () => {
	for (const name of [
		'index',
		'whitepaper',
		'benchmark',
		'illusions',
		'login',
		'join',
		'setup',
		'reset',
		'recover',
		'shared',
		'legal',
		'privacy',
		'hero-preview',
		'collage-preview'
	]) {
		const html = await built(name);
		assert.equal((html.match(/<footer class="horizon\b/g) ?? []).length, 1, name);
		const footer = html.slice(html.indexOf('<footer class="horizon'), html.indexOf('</footer>'));
		for (const landmark of [
			'class="top ',
			'class="columns ',
			'class="trust ',
			'class="band ',
			'class="wordmark ',
			'class="bottom '
		]) {
			assert.ok(footer.includes(landmark), `${name}: ${landmark}`);
		}
		assert.ok(
			footer.indexOf('class="bottom ') < footer.indexOf('class="band '),
			`${name}: notice above artwork`
		);
		assert.ok(!footer.includes('class="to-top '), `${name}: no back-to-top button`);
	}
});

test('setup prerenders as an unavailable card with no form', async () => {
	const html = await built('setup');
	assert.match(html, /<h1[^>]*>[^<]*Set up[^<]*<\/h1>/);
	assert.ok(html.includes('Sign-in is available on a self-hosted or cloud studio install'));
	assert.doesNotMatch(html, /<form\b/);
	assert.doesNotMatch(html, /id="setup-email"/);
});

test('the studio has no page footer', async () => {
	assert.doesNotMatch(await built('app'), /<footer class="horizon\b/);
});

test('the landing preview has no Show tour launcher', async () => {
	assert.doesNotMatch(await built('app'), /Show tour/);
});

test('headers lead to sign in or the waitlist, and login has no kicker', async () => {
	for (const name of ['index', 'whitepaper']) {
		const html = await built(name);
		const header = html.match(/<header\b[\s\S]*?<\/header>/)?.[0];
		assert.ok(header, name);
		const expected = process.env.PUBLIC_WAITLIST_URL ? './login?tab=waitlist' : './login';
		assert.ok(header.includes(`href="${expected}"`), `${name}: expected ${expected}`);
	}
	assert.doesNotMatch(await built('login'), /class="kicker\b/);
});

test('language controls have named flag buttons', async () => {
	for (const name of ['whitepaper', 'login', 'legal']) {
		const html = await built(name);
		for (const [lang, label] of [
			['en', 'English'],
			['es', 'Espa\u00f1ol']
		]) {
			assert.match(
				html,
				new RegExp(`lang="${lang}" aria-label="${label}" title="${label}"[^>]*>[\\s\\S]*?<svg`)
			);
		}
	}
});

test('the install command uses the copied official redirect', async () => {
	const html = await built('index');
	assert.ok(html.includes('curl -fsSL https://potocolom.leonfuller.com/install.sh | bash'));
	assert.equal(
		await readFile(new URL('../build/_redirects', import.meta.url), 'utf8'),
		'/install.sh https://github.com/Portocolom-Studio/potocolom/releases/latest/download/install.sh 302\n'
	);
});

// Browser checks for the prompt marquee on the built home page. The build uses
// root-relative asset URLs, so it is served over HTTP.
const landingBuild = fileURLToPath(new URL('../build', import.meta.url));
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
const promptSelector = '#work button[data-prompt]';
// The gallery wall in the same section has pinned tiles with aria-pressed too.
const pauseSelector = '#work button[aria-pressed]:not([data-prompt]):not(.work-wall button)';
let landing;

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

async function landingBrowser() {
	if (!landing) {
		const server = createServer(async (request, response) => {
			try {
				const pathname = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
				const clean = resolve(landingBuild, `.${pathname}`);
				if (clean !== landingBuild && !clean.startsWith(landingBuild + sep)) {
					response.writeHead(404).end();
					return;
				}
				let found;
				for (const candidate of [clean, `${clean}.html`, join(clean, 'index.html')]) {
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
		await new Promise((resolveListen) => server.listen(0, '127.0.0.1', resolveListen));
		const browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-gpu']
		});
		landing = { server, browser, url: `http://127.0.0.1:${server.address().port}` };
	}
	return landing;
}

after(async () => {
	if (!landing) return;
	await landing.browser.close();
	landing.server.close();
});

async function openLanding({ locale = 'en', reducedMotion = false } = {}) {
	const { browser, url } = await landingBrowser();
	const page = await browser.newPage();
	// localStorage is shared by every page in the browser, so set the locale on each load.
	await page.evaluateOnNewDocument((value) => {
		try {
			localStorage.setItem('locale', value);
		} catch {}
	}, locale);
	if (reducedMotion) {
		await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
	}
	await page.goto(`${url}/`, { waitUntil: 'networkidle0' });
	await page.waitForFunction((value) => document.documentElement.lang === value, {}, locale);
	// The entrance loader keeps the home sections inert until its assets are ready.
	await page.waitForFunction(() => {
		const work = document.querySelector('#work');
		return work && !work.closest('[inert]');
	});
	return page;
}

async function promptTexts() {
	const source = await readFile(
		new URL('../src/lib/prompt-marquee-prompts.ts', import.meta.url),
		'utf8'
	);
	return [...source.matchAll(/primary: '([^']+)'/g)].map((match) => match[1]);
}

function playStates(page) {
	return page.$$eval('#work .prompt-marquee-track', (tracks) =>
		tracks.map((track) => getComputedStyle(track).animationPlayState)
	);
}

// Chrome's computed accessible names for the elements matching selector, in
// document order. Handles from page.$ cannot be used here: a CDP session has
// its own object ids, so the lookup starts from the DOM instead.
async function accessibleNames(page, selector) {
	const cdp = await page.createCDPSession();
	try {
		const { root } = await cdp.send('DOM.getDocument', { depth: 0 });
		const { nodeIds } = await cdp.send('DOM.querySelectorAll', {
			nodeId: root.nodeId,
			selector
		});
		const names = [];
		for (const nodeId of nodeIds) {
			const { node } = await cdp.send('DOM.describeNode', { nodeId });
			const { nodes } = await cdp.send('Accessibility.getPartialAXTree', {
				backendNodeId: node.backendNodeId,
				fetchRelatives: false
			});
			names.push(nodes[0]?.name?.value ?? '');
		}
		return names;
	} finally {
		await cdp.detach();
	}
}

// Tabs from the top of the page and records each stop inside the marquee.
async function marqueeTabStops(page) {
	const here = () =>
		page.evaluate(() => {
			const el = document.activeElement;
			// The gallery wall above the marquee has its own tab stops.
			if (!el?.closest('#work') || el.closest('.work-wall')) return null;
			const text = el.textContent.replace(/\s+/g, ' ').trim();
			return el.hasAttribute('data-prompt')
				? { prompt: text }
				: { control: text || el.getAttribute('aria-label') };
		});
	let stop = null;
	for (let press = 0; press < 300 && !stop; press++) {
		await page.keyboard.press('Tab');
		stop = await here();
	}
	assert.ok(stop, 'keyboard focus must reach the marquee');
	const stops = [];
	while (stop && stops.length < 100) {
		stops.push(stop);
		await page.keyboard.press('Tab');
		stop = await here();
	}
	return stops;
}

test('each marquee prompt has one keyboard stop, and the pause control is the only other', async () => {
	const page = await openLanding();
	try {
		const prompts = await promptTexts();
		const stops = await marqueeTabStops(page);
		const promptStops = stops
			.filter((stop) => stop.prompt !== undefined)
			.map((stop) => prompts.find((text) => stop.prompt.includes(text)) ?? stop.prompt);
		assert.equal(
			promptStops.length,
			prompts.length,
			`${promptStops.length} prompt stops for ${prompts.length} prompts`
		);
		assert.deepEqual([...promptStops].sort(), [...prompts].sort());
		assert.equal(stops.length - promptStops.length, 1, 'one pause control stop');
		// Loop copies stay out of the tab order and the accessibility tree.
		const copies = await page.$$eval('#work [data-copy]', (els) =>
			els.map((el) => [el.getAttribute('aria-hidden'), el.getAttribute('tabindex')])
		);
		assert.equal(copies.length, prompts.length, 'one loop copy per prompt');
		for (const attrs of copies) assert.deepEqual(attrs, ['true', '-1']);
	} finally {
		await page.close();
	}
});

test('each prompt name includes its visible prompt and the copy action', async () => {
	const prompts = await promptTexts();
	for (const [locale, action] of [
		['en', /\bcopy\b/i],
		['es', /copiar/i]
	]) {
		const page = await openLanding({ locale });
		try {
			const copies = await page.$$eval(promptSelector, (buttons) =>
				buttons.map((button) => ({
					text: button.textContent.replace(/\s+/g, ' ').trim(),
					// Loop copies sit under aria-hidden and are not named here.
					hidden: Boolean(button.closest('[aria-hidden="true"]'))
				}))
			);
			const names = await accessibleNames(page, promptSelector);
			assert.equal(names.length, copies.length, `${locale}: names for every prompt button`);
			assert.ok(
				copies.some((copy) => !copy.hidden),
				`${locale}: visible prompts`
			);
			copies.forEach(({ text, hidden }, index) => {
				if (hidden) return;
				const name = names[index];
				const prompt = prompts.find((value) => text.includes(value));
				assert.ok(name.includes(prompt), `${locale}: "${name}" lacks "${prompt}"`);
				assert.match(name, action, `${locale}: "${name}" lacks the copy action`);
			});
		} finally {
			await page.close();
		}
	}
});

test('focus inside a prompt pauses its row until focus leaves', async () => {
	const page = await openLanding();
	try {
		const first = await page.$(promptSelector);
		assert.ok(first, 'a prompt button');
		await first.focus();
		const focusedRow = await page.evaluate(() => {
			const track = document.activeElement?.closest('.prompt-marquee-track');
			return track ? getComputedStyle(track).animationPlayState : null;
		});
		assert.equal(focusedRow, 'paused', 'focused row must pause');
		await page.evaluate(() => document.activeElement?.blur());
		assert.deepEqual(await playStates(page), ['running', 'running'], 'blur resumes both rows');
	} finally {
		await page.close();
	}
});

// Moves the pointer to a viewport point that no marquee row covers, so no row is hovered.
async function parkPointer(page) {
	const point = await page.evaluate(() => {
		const masks = [...document.querySelectorAll('.prompt-marquee-mask')].map((el) =>
			el.getBoundingClientRect()
		);
		for (let y = 0; y < innerHeight; y += 10) {
			for (let x = 0; x < innerWidth; x += 10) {
				if (masks.every((r) => x < r.left || x > r.right || y < r.top || y > r.bottom)) {
					return { x, y };
				}
			}
		}
		return null;
	});
	assert.ok(point, 'a viewport point outside the marquee');
	await page.mouse.move(point.x, point.y);
}

test('the pause control keeps every row paused until it is pressed again', async () => {
	const page = await openLanding();
	try {
		const toggle = await page.$(pauseSelector);
		assert.ok(toggle, 'a pause control in the marquee');
		const pressed = () => toggle.evaluate((el) => el.getAttribute('aria-pressed'));
		assert.equal(await pressed(), 'false');

		await toggle.focus();
		await page.keyboard.press('Enter');
		assert.equal(await pressed(), 'true');
		assert.deepEqual(await playStates(page), ['paused', 'paused']);

		await page.evaluate(() => document.activeElement?.blur());
		assert.deepEqual(await playStates(page), ['paused', 'paused'], 'blur keeps rows paused');

		const first = await page.$(promptSelector);
		await first.focus();
		assert.deepEqual(await playStates(page), ['paused', 'paused'], 'focus keeps rows paused');
		// Blur before hovering, so the hover check cannot pass because of focus.
		await page.evaluate(() => document.activeElement?.blur());
		const box = await first.boundingBox();
		await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
		assert.deepEqual(await playStates(page), ['paused', 'paused'], 'hover keeps rows paused');
		await parkPointer(page);
		assert.deepEqual(await playStates(page), ['paused', 'paused'], 'leaving keeps rows paused');

		// Park the pointer after the last scroll, since a scroll moves the content under it.
		await toggle.focus();
		await parkPointer(page);
		await page.keyboard.press('Enter');
		assert.equal(await pressed(), 'false');
		assert.deepEqual(await playStates(page), ['running', 'running']);
	} finally {
		await page.close();
	}
});

test('hovering a row pauses that row only while the pointer is over it', async () => {
	const page = await openLanding();
	try {
		const mask = await page.$('#work .prompt-marquee-mask');
		assert.ok(mask, 'the first marquee row');
		await mask.evaluate((el) => el.scrollIntoView({ block: 'center' }));
		const box = await mask.boundingBox();
		await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
		assert.deepEqual(await playStates(page), ['paused', 'running'], 'hovered row pauses alone');
		await parkPointer(page);
		assert.deepEqual(await playStates(page), ['running', 'running'], 'leaving resumes both rows');
	} finally {
		await page.close();
	}
});

test('the pause control is named in English and Spanish, with reduced motion on and off', async () => {
	for (const [locale, pause] of [
		['en', 'Pause prompts'],
		['es', 'Pausar prompts']
	]) {
		for (const reducedMotion of [false, true]) {
			const page = await openLanding({ locale, reducedMotion });
			try {
				const toggle = await page.$(pauseSelector);
				assert.ok(toggle, `${locale}: a pause control in the marquee`);
				const [label] = await accessibleNames(page, pauseSelector);
				assert.equal(label, pause, `${locale} reduced motion ${reducedMotion}`);
				await toggle.focus();
				await page.keyboard.press('Enter');
				// A toggle keeps its name; aria-pressed carries the state.
				assert.deepEqual(await accessibleNames(page, pauseSelector), [pause]);
				assert.equal(
					await page.$eval(pauseSelector, (el) => el.getAttribute('aria-pressed')),
					'true',
					`${locale} reduced motion ${reducedMotion} pressed`
				);
			} finally {
				await page.close();
			}
		}
	}
});

test('a visible loop copy copies its prompt when clicked', async () => {
	const prompts = await promptTexts();
	const page = await openLanding();
	try {
		await page.evaluate(() => {
			window.__copied = [];
			Object.defineProperty(navigator, 'clipboard', {
				configurable: true,
				value: {
					writeText: async (text) => {
						window.__copied.push(text);
					}
				}
			});
		});
		// Pause first so the copy stays under the pointer.
		const toggle = await page.$(pauseSelector);
		await toggle.focus();
		await page.keyboard.press('Enter');
		const copy = await page.$('#work [data-copy]');
		assert.ok(copy, 'a loop copy');
		await copy.evaluate((el) => el.scrollIntoView({ block: 'center', inline: 'center' }));
		const box = await copy.boundingBox();
		const x = box.x + box.width / 2;
		const y = box.y + box.height / 2;
		assert.ok(
			await copy.evaluate((el, px, py) => el.contains(document.elementFromPoint(px, py)), x, y),
			'the loop copy is under the pointer'
		);
		await page.mouse.click(x, y);
		await page.waitForFunction(() => window.__copied.length > 0, { timeout: 3000 });
		const [text] = await page.evaluate(() => window.__copied);
		const shown = await copy.evaluate((el) => el.textContent);
		const prompt = prompts.find((value) => text.startsWith(value));
		assert.ok(prompt && shown.includes(prompt), `copied "${text}" for "${shown.trim()}"`);
	} finally {
		await page.close();
	}
});

test('reduced motion stops the marquee rows and hides the loop copies', async () => {
	const prompts = await promptTexts();
	for (const reducedMotion of [false, true]) {
		const page = await openLanding({ reducedMotion });
		try {
			const names = await page.$$eval('#work .prompt-marquee-track', (tracks) =>
				tracks.map((track) => getComputedStyle(track).animationName)
			);
			assert.equal(names.length, 2, 'two rows');
			for (const name of names) {
				assert.equal(name === 'none', reducedMotion, `reduced motion ${reducedMotion}: ${name}`);
			}
			const copies = await page.$$eval('#work [data-copy]', (els) =>
				els.map((el) => getComputedStyle(el).display)
			);
			assert.equal(copies.length, prompts.length, 'one loop copy per prompt');
			for (const display of copies) {
				assert.equal(
					display === 'none',
					reducedMotion,
					`reduced motion ${reducedMotion}: ${display}`
				);
			}
		} finally {
			await page.close();
		}
	}
});
