import assert from 'node:assert/strict';
import { access, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import puppeteer from 'puppeteer-core';

const build = resolve(process.argv[2] ?? fileURLToPath(new URL('../build', import.meta.url)));
const WAIT_MS = 20000;
const REPORT_RENDER_MEASURE = process.env.REPORT_RENDER_MEASURE === '1';
const pause = (milliseconds) =>
	new Promise((resolvePause) => setTimeout(resolvePause, milliseconds));
const contentTypes = {
	'.html': 'text/html',
	'.js': 'application/javascript',
	'.css': 'text/css',
	'.json': 'application/json',
	'.svg': 'image/svg+xml',
	'.woff2': 'font/woff2'
};

const modelIds = Array.from({ length: 9 }, (_, index) => `benchmark-model-${index + 1}`);
const sessions = [
	{
		id: 'session-one',
		created_at: '2026-10-27T10:00:00.000Z',
		targetVramGb: 16,
		modelIds,
		variant: 'a'
	},
	{
		id: 'session-two',
		created_at: '2026-10-27T11:00:00.000Z',
		targetVramGb: 24,
		modelIds,
		variant: 'b'
	},
	{
		id: 'session-three',
		created_at: '2026-10-27T12:00:00.000Z',
		targetVramGb: 32,
		modelIds: ['alternate-model-1', 'alternate-model-2', 'alternate-model-3', 'alternate-model-4'],
		variant: 'c'
	},
	{
		id: 'session-four',
		created_at: '2026-10-27T13:00:00.000Z',
		targetVramGb: 40,
		modelIds: ['retry-model-1', 'retry-model-2', 'retry-model-3'],
		variant: 'c'
	}
];
const accountFixtures = {
	A: {
		id: '11000000-0000-4000-8000-000000000001',
		email: 'owner-a@example.test',
		role: 'admin'
	},
	B: {
		id: '22000000-0000-4000-8000-000000000002',
		email: 'owner-b@example.test',
		role: 'admin'
	}
};

function reportFor(session) {
	const reportModelIds = session.modelIds ?? modelIds;
	const promptCount = session.variant === 'c' ? 36 : 120;
	const modelStats = reportModelIds.map((modelId, index) => ({
		model_id: modelId,
		succeeded: promptCount,
		failed: 0,
		load_ms: 15000 + index * 500,
		avg_gpu_ms:
			session.variant === 'b'
				? 3200 - index * 271 + session.targetVramGb
				: session.variant === 'c'
					? 2300 + index * 411
					: 900 + index * 137 + session.targetVramGb,
		median_gpu_ms:
			session.variant === 'b'
				? 2900 - index * 233
				: session.variant === 'c'
					? 2100 + index * 373
					: 850 + index * 131,
		avg_wall_s:
			session.variant === 'b'
				? 10.2 - index * 0.7
				: session.variant === 'c'
					? 1.1 + index * 1.4
					: 3.2 + index * 0.3
	}));
	const results = reportModelIds.flatMap((modelId, modelIndex) =>
		Array.from({ length: promptCount }, (_, resultIndex) => ({
			prompt_id: resultIndex,
			title: `Prompt ${resultIndex}`,
			category: `Category ${resultIndex % 6}`,
			model_id: modelId,
			variant: 'default',
			params: {},
			model_load_ms: modelIndex === 0 && resultIndex === 0 ? 15000 : 0,
			state: 'succeeded',
			gpu_ms:
				session.variant === 'b'
					? 250 + ((modelIndex * 389 + resultIndex * 97 + 1700) % 3400)
					: session.variant === 'c'
						? 400 + ((modelIndex * 503 + resultIndex * 191) % 2400)
						: 700 + ((modelIndex * 113 + resultIndex * 37) % 2900),
			wall_s:
				session.variant === 'b'
					? 0.7 + ((modelIndex * 17 + resultIndex * 23) % 61) / 10
					: session.variant === 'c'
						? 1.2 + ((modelIndex * 19 + resultIndex * 29) % 38) / 10
						: 2.1 + ((modelIndex * 13 + resultIndex * 11) % 27) / 10
		}))
	);
	return {
		created_at: session.created_at,
		target_vram_gb: session.targetVramGb,
		prompt_count: promptCount,
		models: reportModelIds,
		variants_per_prompt: 1,
		total_jobs: results.length,
		succeeded: results.length,
		failed: 0,
		model_stats: modelStats,
		results
	};
}

function expectedLeaderboardModelIds(report) {
	return [...report.model_stats]
		.sort((left, right) => left.avg_gpu_ms - right.avg_gpu_ms)
		.map((stat) => stat.model_id);
}

const reports = new Map(sessions.map((session) => [session.id, reportFor(session)]));
let publishedSessions = sessions.slice(0, 2);
const requestLog = [];
const errorLog = [];
const completedApiLog = [];
const apiFailures = { sessions: false, staticReport: false, reportIds: new Set() };
const apiGates = new Map();
const activeApiGates = new Set();
let activeAccountFixture = null;

function deferred() {
	let resolve;
	const promise = new Promise((done) => (resolve = done));
	return { promise, resolve };
}

function deferNextApi(path) {
	const started = deferred();
	const reply = deferred();
	const queue = apiGates.get(path) ?? [];
	const gate = {
		started,
		reply,
		release: (answer = apiReply(path)) => {
			reply.resolve(answer);
			activeApiGates.delete(gate);
		}
	};
	queue.push(gate);
	apiGates.set(path, queue);
	activeApiGates.add(gate);
	return { started: started.promise, release: gate.release };
}

function resetApiFixtures() {
	for (const gate of [...activeApiGates]) gate.release();
	publishedSessions = sessions.slice(0, 2);
	apiFailures.sessions = false;
	apiFailures.staticReport = false;
	apiFailures.reportIds = new Set();
	activeAccountFixture = null;
	apiGates.clear();
}

function releasePendingApiGates() {
	for (const gate of [...activeApiGates]) gate.release();
}

function apiReply(path) {
	if (path === '/api/v1/account') {
		return activeAccountFixture
			? { status: 200, body: activeAccountFixture }
			: { status: 404, body: { detail: 'No account mode' } };
	}
	if (path === '/api/v1/config')
		return { status: 200, body: { auth_methods: [], billing_enabled: false } };
	if (path === '/api/v1/benchmark/sessions') {
		if (apiFailures.sessions) return { status: 503, body: { detail: 'sessions unavailable' } };
		return {
			status: 200,
			body: publishedSessions.map(({ id, created_at }) => ({ id, created_at }))
		};
	}
	const reportMatch = path.match(/^\/api\/v1\/benchmark\/sessions\/([^/]+)$/);
	if (reportMatch) {
		if (apiFailures.reportIds.has(reportMatch[1]))
			return { status: 503, body: { detail: 'report unavailable' } };
		const report = reports.get(reportMatch[1]);
		return report ? { status: 200, body: report } : { status: 404, body: {} };
	}
	if (path === '/api/v1/models' || path === '/api/v1/generations') {
		return { status: 200, body: [] };
	}
	if (path === '/api/v1/generations/models') return { status: 200, body: { models: [] } };
	if (path === '/api/v1/metrics/gpu/history') {
		return { status: 200, body: { from: '', to: '', rollup: 'raw', samples: [] } };
	}
	return { status: 200, body: [] };
}

async function serveBuild() {
	resetApiFixtures();
	requestLog.length = 0;
	errorLog.length = 0;
	completedApiLog.length = 0;
	const server = createServer(async (request, response) => {
		try {
			const url = new URL(request.url, 'http://localhost');
			if (url.pathname.startsWith('/api/')) {
				requestLog.push(`${url.pathname}${url.search}`);
				const gate = apiGates.get(url.pathname)?.shift();
				let answer;
				if (gate) {
					gate.started.resolve();
					answer = await gate.reply.promise;
					if (response.destroyed) return;
				} else {
					answer = apiReply(url.pathname);
				}
				if (answer.status >= 400) errorLog.push(`${url.pathname}:${answer.status}`);
				completedApiLog.push(url.pathname);
				response.writeHead(answer.status, { 'Content-Type': 'application/json' });
				response.end(JSON.stringify(answer.body));
				return;
			}
			if (url.pathname === '/benchmark/results.json' && apiFailures.staticReport) {
				errorLog.push('/benchmark/results.json:503');
				response.writeHead(503, { 'Content-Type': 'application/json' });
				response.end('{}');
				return;
			}
			const file = resolve(build, `.${decodeURIComponent(url.pathname)}`);
			if (file !== build && !file.startsWith(`${build}/`)) {
				response.writeHead(404).end();
				return;
			}
			const candidates = [
				file,
				`${file}.html`,
				join(file, 'index.html'),
				join(build, '200.html'),
				join(build, 'index.html')
			];
			let found;
			for (const candidate of candidates) {
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
	return server;
}

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

async function waitForText(page, text) {
	await page.waitForFunction(
		(expected) => document.body.innerText.includes(expected),
		{ timeout: WAIT_MS },
		text
	);
}

async function waitForActiveViewTransition(page) {
	await page.waitForFunction(
		async () => {
			const transition = document.activeViewTransition;
			if (transition) await transition.finished.catch(() => {});
			return document.activeViewTransition === null;
		},
		{ timeout: WAIT_MS }
	);
}

async function clickButton(page, text) {
	const clicked = await page.evaluate((expected) => {
		const button = [...document.querySelectorAll('button')].find(
			(element) => element.innerText.trim() === expected && !element.disabled
		);
		if (!button) return false;
		button.click();
		return true;
	}, text);
	assert.ok(clicked, `button ${text} exists and is enabled`);
}

async function selectBenchmarkSession(page, expectedId) {
	await page.waitForSelector('[data-slot="select-trigger"]', { visible: true, timeout: WAIT_MS });
	await page.focus('[data-slot="select-trigger"]');
	await page.keyboard.press('Enter');
	try {
		await page.waitForFunction(
			(id) =>
				[...document.querySelectorAll('[data-slot="select-item"]')].some(
					(option) => option.getAttribute('data-value') === id
				),
			{ timeout: WAIT_MS },
			expectedId
		);
	} catch (error) {
		const state = await page.evaluate(() => ({
			options: [...document.querySelectorAll('[data-slot="select-item"]')].map((item) =>
				item.getAttribute('data-value')
			),
			triggers: [...document.querySelectorAll('[data-slot="select-trigger"]')].map((item) => ({
				text: item.textContent?.trim(),
				expanded: item.getAttribute('aria-expanded'),
				disabled: item.getAttribute('aria-disabled'),
				rect: (() => {
					const rect = item.getBoundingClientRect();
					return { width: rect.width, height: rect.height };
				})()
			}))
		}));
		throw new Error(
			`Benchmark session option ${expectedId} not present; state: ${JSON.stringify(state)}`,
			{
				cause: error
			}
		);
	}
	const optionSelector = `[data-slot="select-item"][data-value="${expectedId}"]`;
	await page.waitForSelector(optionSelector, { visible: true, timeout: WAIT_MS });
	await page.click(optionSelector);
	await page.waitForFunction(
		() => document.querySelectorAll('[data-slot="select-item"]').length === 0,
		{ timeout: WAIT_MS }
	);
}

async function waitForApiLogCount(log, path, expected) {
	const deadline = Date.now() + WAIT_MS;
	while (log.filter((entry) => entry === path).length < expected) {
		assert.ok(Date.now() < deadline, `API path reached expected count: ${path}`);
		await pause(25);
	}
}

async function waitForBenchmarkReport(page, targetVramGb, modelCount) {
	await page.waitForFunction(
		({ expectedVram, expectedModels }) => {
			const badge = [...document.querySelectorAll('span')].find(
				(element) => element.textContent.trim() === `${expectedVram} GB VRAM`
			);
			const table = document.querySelector('[data-benchmark-table]');
			return (
				badge !== undefined &&
				table?.querySelectorAll('tbody tr').length === expectedModels &&
				document.querySelector('[data-benchmark-chart="bars"]') !== null &&
				document.querySelector('[data-benchmark-chart="timeline"]') !== null
			);
		},
		{ timeout: WAIT_MS },
		{ expectedVram: targetVramGb, expectedModels: modelCount }
	);
}

async function renderedBenchmarkReport(page) {
	return page.evaluate(() => {
		const badge = [...document.querySelectorAll('span')].find((element) =>
			/^\d+ GB VRAM$/.test(element.textContent.trim())
		);
		const table = document.querySelector('[data-benchmark-table]');
		const charts = [...document.querySelectorAll('[data-benchmark-chart]')].map((chart) => ({
			kind: chart.getAttribute('data-benchmark-chart'),
			paths: [...chart.querySelectorAll('svg path')].map((path) => path.getAttribute('d') ?? '')
		}));
		return {
			reportId:
				document.querySelector('[data-benchmark-report]')?.getAttribute('data-benchmark-report') ??
				null,
			badge: badge?.textContent.trim() ?? null,
			rows: [...(table?.querySelectorAll('tbody tr') ?? [])].map((row) =>
				[...row.querySelectorAll('td')].map((cell) => cell.textContent.trim())
			),
			charts
		};
	});
}

async function installReportMotionRecorder(page) {
	await page.evaluateOnNewDocument(() => {
		window.__reportMotionStarts = [];
		const reportRoot = () => {
			const marked = document.querySelector('[data-benchmark-report]');
			if (marked) return marked;
			const parts = [
				document.querySelector('[data-benchmark-chart="bars"]'),
				document.querySelector('[data-benchmark-chart="timeline"]'),
				document.querySelector('[data-benchmark-table]')
			].filter(Boolean);
			if (parts.length === 0) return null;
			for (let candidate = parts[0]; candidate; candidate = candidate.parentElement) {
				if (parts.every((part) => candidate.contains(part))) return candidate;
			}
			return null;
		};
		document.addEventListener(
			'animationstart',
			(event) => {
				const root = reportRoot();
				if (!root || !(event.target instanceof Element) || !root.contains(event.target)) return;
				const animations = root
					.getAnimations({ subtree: true })
					.filter((item) => item.playState === 'running');
				window.__reportMotionStarts.push({
					animationCount: animations.length,
					keyframes: animations.flatMap(
						(animation) =>
							animation.effect?.getKeyframes().map((frame) => ({
								opacity: frame.opacity ?? null,
								transform: frame.transform ?? null
							})) ?? []
					)
				});
			},
			true
		);
	});
}

async function reportMotionState(page) {
	return page.evaluate(() => {
		const marked = document.querySelector('[data-benchmark-report]');
		const parts = [
			document.querySelector('[data-benchmark-chart="bars"]'),
			document.querySelector('[data-benchmark-chart="timeline"]'),
			document.querySelector('[data-benchmark-table]')
		].filter(Boolean);
		let root = marked;
		if (!root && parts.length > 0) {
			for (let candidate = parts[0]; candidate; candidate = candidate.parentElement) {
				if (parts.every((part) => candidate.contains(part))) {
					root = candidate;
					break;
				}
			}
		}
		if (!root)
			return { rootPresent: false, activeCount: 0, starts: window.__reportMotionStarts ?? [] };
		return {
			rootPresent: true,
			markedRootCount: document.querySelectorAll('[data-benchmark-report]').length,
			reportId: root.getAttribute('data-benchmark-report'),
			activeCount: root
				.getAnimations({ subtree: true })
				.filter((item) => item.playState === 'running').length,
			starts: window.__reportMotionStarts ?? [],
			animationName: getComputedStyle(root).animationName,
			animationFillMode: getComputedStyle(root).animationFillMode
		};
	});
}

async function finishReportMotion(page) {
	await page.evaluate(async () => {
		const root = document.querySelector('[data-benchmark-report]');
		await Promise.all(
			root
				?.getAnimations({ subtree: true })
				.map((animation) => animation.finished.catch(() => {})) ?? []
		);
	});
}

async function installRenderRecorder(page) {
	await page.evaluateOnNewDocument(() => {
		window.__viewMeasures = [];
		window.__viewMeasureActive = false;
		window.__longTasks = [];
		if ('PerformanceObserver' in window) {
			try {
				new PerformanceObserver((list) => {
					for (const entry of list.getEntries()) window.__longTasks.push(entry.duration);
				}).observe({ type: 'longtask', buffered: true });
			} catch {}
		}
	});
}

async function startRenderMeasure(page) {
	await page.evaluate(() => {
		const startedAt = performance.now();
		const record = {
			startedAt,
			rowsAndChartsVisibleMs: null,
			finalGeometryQuiet200Ms: null,
			finalDataWork: null,
			geometryMutations: 0
		};
		window.__viewMeasures.push(record);
		window.__viewMeasureActive = true;
		let lastChangeAt = startedAt;
		let observer;
		const sample = () => {
			if (!window.__viewMeasureActive) return;
			const benchmarkTab = new URL(location.href).searchParams.get('tab') === 'benchmarks';
			const cardTitles = [...document.querySelectorAll('[data-slot="card-title"]')];
			const chartTitles = ['GPU vs wall per model', 'Run timeline'].map((text) =>
				cardTitles.find((title) => title.textContent?.includes(text))
			);
			const chartRoots = chartTitles
				.map((title) => title?.closest('[data-slot="card"]'))
				.filter((root) => root !== null && root !== undefined);
			const visibleCharts = chartRoots.filter((root) => {
				const rect = root.getBoundingClientRect();
				return rect.width > 0 && rect.height > 0;
			});
			const benchmarkRegion = [...document.querySelectorAll('[role="region"]')].find((region) =>
				/benchmark table/i.test(region.getAttribute('aria-label') ?? '')
			);
			const rows = benchmarkRegion?.querySelectorAll('tbody tr').length ?? 0;
			if (
				benchmarkTab &&
				rows >= 9 &&
				visibleCharts.length === 2 &&
				record.rowsAndChartsVisibleMs === null
			) {
				record.rowsAndChartsVisibleMs = performance.now() - startedAt;
				lastChangeAt = performance.now();
				observer = new MutationObserver((mutations) => {
					const geometryMutations = mutations.filter(
						(mutation) =>
							mutation.type === 'childList' ||
							(mutation.type === 'attributes' &&
								['d', 'x', 'y', 'width', 'height', 'transform'].includes(
									mutation.attributeName ?? ''
								))
					);
					if (geometryMutations.length > 0) {
						record.geometryMutations += geometryMutations.length;
						lastChangeAt = performance.now();
					}
				});
				for (const root of visibleCharts) {
					observer.observe(root, {
						attributes: true,
						childList: true,
						subtree: true,
						attributeFilter: ['d', 'x', 'y', 'width', 'height', 'transform']
					});
				}
			}
			if (record.rowsAndChartsVisibleMs !== null && performance.now() - lastChangeAt >= 200) {
				const chartMarks = chartRoots.map((root) => {
					const title = root.querySelector('[data-slot="card-title"]')?.textContent?.trim() ?? '';
					const requiredMarks =
						title === 'GPU vs wall per model' ? 18 : title === 'Run timeline' ? 9 : 0;
					const paths = [...root.querySelectorAll('svg path')];
					const finiteNondegeneratePathCount = paths.filter((path) => {
						const data = path.getAttribute('d')?.trim() ?? '';
						const coordinates =
							data.match(/-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?/gi)?.map(Number) ?? [];
						if (coordinates.length < 4 || coordinates.some((value) => !Number.isFinite(value))) {
							return false;
						}
						const bounds = path.getBBox();
						return (
							[bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite) &&
							bounds.width > 0 &&
							bounds.height > 0
						);
					}).length;
					return { title, pathCount: paths.length, finiteNondegeneratePathCount, requiredMarks };
				});
				const finalDataReady =
					benchmarkTab &&
					rows === 9 &&
					chartMarks.length === 2 &&
					chartMarks.every(
						(chart) =>
							chart.requiredMarks > 0 && chart.finiteNondegeneratePathCount >= chart.requiredMarks
					);
				if (finalDataReady) {
					record.finalDataWork = { modelRows: rows, charts: chartMarks };
					record.finalGeometryQuiet200Ms = performance.now() - startedAt;
					window.__viewMeasureActive = false;
					observer?.disconnect();
					return;
				}
			}
			requestAnimationFrame(sample);
		};
		requestAnimationFrame(sample);
	});
}

function watchBrowser(page) {
	const log = { responses: [], requestFailures: [], pageErrors: [], consoleErrors: [] };
	page.on('response', (response) => {
		log.responses.push({ status: response.status(), url: response.url() });
	});
	page.on('requestfailed', (request) => {
		log.requestFailures.push({ url: request.url(), error: request.failure()?.errorText });
	});
	page.on('pageerror', (error) => log.pageErrors.push(error.stack ?? error.message));
	page.on('console', (message) => {
		if (message.type() === 'error') log.consoleErrors.push(message.text());
	});
	return log;
}

function assertBrowserClean(log) {
	assert.deepEqual(log.pageErrors, [], `browser page errors: ${JSON.stringify(log.pageErrors)}`);
	assert.deepEqual(
		log.requestFailures,
		[],
		`failed browser requests: ${JSON.stringify(log.requestFailures)}`
	);
	const unexpectedStatuses = log.responses.filter(({ status, url }) => {
		if (status < 400) return false;
		const path = new URL(url).pathname;
		return !(
			(path === '/api/v1/account' && status === 404) ||
			(path === '/api/v1/benchmark/sessions' && status === 503)
		);
	});
	assert.deepEqual(
		unexpectedStatuses,
		[],
		`unexpected HTTP error responses: ${JSON.stringify(unexpectedStatuses)}`
	);
	const staticReportFailures = log.responses.filter(
		({ status, url }) => new URL(url).pathname === '/benchmark/results.json' && status >= 400
	);
	assert.deepEqual(staticReportFailures, [], 'static benchmark report fallback must not fail');
}

test('Metrics restores tabs, ranges and sessions with a real benchmark report', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	let page;
	let browserLog;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		page = await browser.newPage();
		browserLog = watchBrowser(page);
		await page.setViewport({ width: 1440, height: 1000 });
		if (REPORT_RENDER_MEASURE) await installRenderRecorder(page);
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForText(page, '16 GB VRAM');
		await waitForText(page, 'GPU vs wall per model');
		await waitForText(page, 'Run timeline');
		assert.equal(reports.get('session-one').results.length, 1080);
		assert.equal(reports.get('session-one').model_stats.length, 9);
		if (REPORT_RENDER_MEASURE) {
			assert.equal(reports.get('session-two').results.length, 1080);
			assert.equal(reports.get('session-two').model_stats.length, 9);
			await selectBenchmarkSession(page, 'session-two');
			await waitForText(page, '24 GB VRAM');
			await clickButton(page, 'Usage');
			await waitForText(page, 'GPU activity');
			await clickButton(page, 'Benchmarks');
			await waitForText(page, '24 GB VRAM');
			const requestStartAt = requestLog.length;
			const errorStartAt = errorLog.length;
			for (let cycle = 0; cycle < 5; cycle += 1) {
				await clickButton(page, 'Usage');
				await waitForText(page, 'GPU activity');
				await startRenderMeasure(page);
				await clickButton(page, 'Benchmarks');
				await page.waitForFunction(
					() => {
						const measure = window.__viewMeasures.at(-1);
						return measure?.finalGeometryQuiet200Ms !== null && measure?.finalDataWork !== null;
					},
					{ timeout: WAIT_MS }
				);
			}
			const measurements = await page.evaluate(() => ({
				cycles: window.__viewMeasures,
				longTasks: window.__longTasks,
				geometryMutationCount: window.__viewMeasures.reduce(
					(total, measure) => total + measure.geometryMutations,
					0
				)
			}));
			assert.equal(measurements.cycles.length, 5);
			for (const [index, measure] of measurements.cycles.entries()) {
				assert.equal(
					measure.finalDataWork?.modelRows,
					9,
					`cycle ${index + 1} rendered nine model rows`
				);
				const charts = new Map(measure.finalDataWork.charts.map((chart) => [chart.title, chart]));
				assert.equal(charts.size, 2, `cycle ${index + 1} recorded both titled charts`);
				assert.ok(
					charts.get('GPU vs wall per model')?.finiteNondegeneratePathCount >= 18,
					`cycle ${index + 1} rendered finite, nondegenerate bars for nine models and two series`
				);
				assert.ok(
					charts.get('Run timeline')?.finiteNondegeneratePathCount >= 9,
					`cycle ${index + 1} rendered finite, nondegenerate paths for nine model series`
				);
			}
			assertBrowserClean(browserLog);
			assert.equal(errorLog.filter((entry) => entry === '/benchmark/results.json:503').length, 0);
			process.stdout.write(
				`${JSON.stringify({
					build,
					fixture: { models: modelIds.length, resultsPerReport: 1080 },
					requestCount: requestLog.length - requestStartAt,
					requests: requestLog.slice(requestStartAt),
					apiErrorCount: errorLog.length - errorStartAt,
					apiErrors: errorLog.slice(errorStartAt),
					measurements
				})}\n`
			);
			return;
		}
		assert.equal(requestLog.filter((path) => path === '/api/v1/benchmark/sessions').length, 1);
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-one').length,
			1
		);

		await selectBenchmarkSession(page, 'session-two');
		await waitForText(page, '24 GB VRAM');
		await clickButton(page, 'Usage');
		await waitForText(page, 'GPU activity');
		await clickButton(page, '24h');
		await clickButton(page, 'Images');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === 'images');
		apiFailures.sessions = true;
		apiFailures.staticReport = true;
		await clickButton(page, 'Metrics');
		await waitForText(page, 'GPU activity');
		await waitForText(page, '24h');
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-two').length,
			1,
			'the Metrics entry reopens the last chosen Usage tab'
		);
		await clickButton(page, 'Benchmarks');
		await waitForText(page, '24 GB VRAM');
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-two').length,
			1,
			'warm revisit reuses the selected report'
		);
		assert.equal(
			errorLog.filter((entry) => entry === '/api/v1/benchmark/sessions:503').length,
			1,
			'a failed session-list refresh leaves the cached report visible'
		);
		assert.equal(errorLog.filter((entry) => entry === '/benchmark/results.json:503').length, 0);
		apiFailures.sessions = false;
		apiFailures.staticReport = false;
		await clickButton(page, 'Retry');
		await page.waitForFunction(
			() => !document.body.innerText.includes('That benchmark session could not be loaded.')
		);
		await waitForText(page, '24 GB VRAM');

		await clickButton(page, 'Usage');
		await waitForText(page, 'GPU activity');
		await page.reload({ waitUntil: 'domcontentloaded' });
		await waitForText(page, 'GPU activity');
		const activeRange = await page.$eval('button', () =>
			[...document.querySelectorAll('button')]
				.find((button) => button.innerText.trim() === '24h')
				?.getAttribute('data-state')
		);
		assert.equal(activeRange, 'on', 'the Usage range survives reload');

		await clickButton(page, 'Benchmarks');
		await waitForText(page, '24 GB VRAM');
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-two').length,
			2,
			'reload refetches the report body once because the in-memory cache was cleared'
		);
		assert.equal(
			errorLog.filter((entry) => entry === '/api/v1/benchmark/sessions:503').length,
			1,
			'the failed session refresh was recorded once'
		);
		assertBrowserClean(browserLog);
	} catch (error) {
		const settled = page
			? await page.evaluate(() => ({
					url: location.href,
					title: document.title,
					text: document.body?.innerText?.slice(0, 12000) ?? '',
					html: document.body?.innerHTML?.slice(0, 40000) ?? ''
				}))
			: null;
		const diagnostic = {
			error: String(error),
			url: page?.url() ?? null,
			requestLog,
			apiErrors: errorLog,
			browserLog,
			settled
		};
		await mkdir(new URL('../../.review/', import.meta.url), { recursive: true });
		await writeFile(
			new URL('../../.review/metrics-browser-failure.json', import.meta.url),
			JSON.stringify(diagnostic, null, 2)
		);
		if (page) {
			await page.screenshot({
				path: fileURLToPath(new URL('../../.review/metrics-browser-failure.png', import.meta.url)),
				fullPage: true
			});
		}
		throw error;
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks warm tab return refreshes sessions once without refetching cached reports', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);
		assert.equal(requestLog.filter((path) => path === '/api/v1/benchmark/sessions').length, 1);
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-one').length,
			1
		);

		await clickButton(page, 'Usage');
		await waitForText(page, 'GPU activity');
		publishedSessions = [...publishedSessions, sessions[2]];
		await clickButton(page, 'Benchmarks');
		await waitForBenchmarkReport(page, 16, 9);
		await waitForApiLogCount(requestLog, '/api/v1/benchmark/sessions', 2);
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-one').length,
			1,
			'warm tab return does not refetch a cached report body'
		);
		await selectBenchmarkSession(page, 'session-three');
		await waitForBenchmarkReport(page, 32, 4);
		const snapshot = await renderedBenchmarkReport(page);
		assert.equal(snapshot.badge, '32 GB VRAM');
		assert.deepEqual(
			snapshot.rows.map((row) => row[1]),
			sessions[2].modelIds,
			'the refreshed session list exposes the new report payload'
		);
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-three').length,
			1
		);
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks report entry animates one report root and respects reduced motion', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
		await installReportMotionRecorder(page);
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);
		await page.waitForFunction(() => window.__reportMotionStarts?.length >= 1, {
			timeout: WAIT_MS
		});
		const initialMotion = await reportMotionState(page);
		assert.equal(initialMotion.rootPresent, true, 'ready report has one animated content root');
		assert.equal(initialMotion.markedRootCount, 1);
		assert.equal(initialMotion.reportId, 'session-one');
		assert.equal(initialMotion.starts.length, 1, 'cold report entry starts one local animation');
		assert.equal(initialMotion.starts[0].animationCount, 1);
		assert.ok(
			initialMotion.starts[0].keyframes.some((frame) => frame.opacity !== null) &&
				initialMotion.starts[0].keyframes.some((frame) => frame.transform !== null),
			'the report root animates opacity and transform rather than chart paths'
		);
		assert.equal(await page.$$eval('[data-benchmark-report]', (roots) => roots.length), 1);

		await selectBenchmarkSession(page, 'session-two');
		assert.equal(
			await page.evaluate(
				() => document.activeElement === document.querySelector('[data-slot="select-trigger"]')
			),
			true,
			'report selection keeps focus on the selector'
		);
		await waitForBenchmarkReport(page, 24, 9);
		await page.waitForFunction(() => window.__reportMotionStarts?.length >= 2, {
			timeout: WAIT_MS
		});
		const switchedMotion = await reportMotionState(page);
		assert.equal(switchedMotion.reportId, 'session-two');
		assert.equal(
			switchedMotion.starts.length,
			2,
			'cached report selection starts one new pane animation'
		);
		assert.equal(await page.$$eval('[data-benchmark-report]', (roots) => roots.length), 1);
		assert.ok(
			switchedMotion.activeCount <= 1,
			'report changes never overlap multiple pane animations'
		);

		await finishReportMotion(page);
		assert.equal(
			(await reportMotionState(page)).activeCount,
			0,
			'finished report entry is no longer running'
		);

		await selectBenchmarkSession(page, 'session-one');
		assert.equal(
			await page.evaluate(
				() => document.activeElement === document.querySelector('[data-slot="select-trigger"]')
			),
			true,
			'cached report selection keeps focus on the selector'
		);
		await waitForBenchmarkReport(page, 16, 9);
		await page.waitForFunction(() => window.__reportMotionStarts?.length >= 3, {
			timeout: WAIT_MS
		});
		const cachedA = await reportMotionState(page);
		assert.equal(cachedA.reportId, 'session-one');
		assert.equal(cachedA.markedRootCount, 1);
		assert.ok(cachedA.activeCount <= 1);
		await finishReportMotion(page);
		assert.equal((await reportMotionState(page)).activeCount, 0);

		await selectBenchmarkSession(page, 'session-two');
		assert.equal(
			await page.evaluate(
				() => document.activeElement === document.querySelector('[data-slot="select-trigger"]')
			),
			true
		);
		await waitForBenchmarkReport(page, 24, 9);
		await page.waitForFunction(() => window.__reportMotionStarts?.length >= 4, {
			timeout: WAIT_MS
		});
		const cachedB = await reportMotionState(page);
		assert.equal(cachedB.reportId, 'session-two');
		assert.equal(cachedB.markedRootCount, 1);
		assert.ok(cachedB.activeCount <= 1);
		await finishReportMotion(page);
		assert.equal((await reportMotionState(page)).activeCount, 0);

		await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
		await selectBenchmarkSession(page, 'session-one');
		await waitForBenchmarkReport(page, 16, 9);
		await page.evaluate(() => new Promise(requestAnimationFrame));
		const reducedMotion = await reportMotionState(page);
		assert.equal(reducedMotion.activeCount, 0, 'reduced motion disables report entry animation');
		assert.equal(reducedMotion.reportId, 'session-one');
		assert.equal(reducedMotion.starts.length, 4, 'reduced motion starts no new report animation');
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks replaces report payloads by selected session and restores fresh geometry', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		publishedSessions = [...sessions];
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);
		const first = await renderedBenchmarkReport(page);

		await selectBenchmarkSession(page, 'session-two');
		await waitForBenchmarkReport(page, 24, 9);
		const second = await renderedBenchmarkReport(page);
		assert.deepEqual(
			[...second.rows.map((row) => row[1])].sort(),
			[...first.rows.map((row) => row[1])].sort()
		);
		assert.notDeepEqual(
			second.rows,
			first.rows,
			'same model IDs show the selected report statistics'
		);
		assert.notDeepEqual(
			second.charts.find((chart) => chart.kind === 'timeline')?.paths,
			first.charts.find((chart) => chart.kind === 'timeline')?.paths,
			'same model IDs render the selected report timeline geometry'
		);

		await selectBenchmarkSession(page, 'session-one');
		await waitForBenchmarkReport(page, 16, 9);
		assert.deepEqual(
			await renderedBenchmarkReport(page),
			first,
			'returning to a cached report restores its own table and chart geometry'
		);

		await selectBenchmarkSession(page, 'session-three');
		await waitForBenchmarkReport(page, 32, 4);
		const third = await renderedBenchmarkReport(page);
		assert.deepEqual([...third.rows.map((row) => row[1])].sort(), [...sessions[2].modelIds].sort());
		assert.notDeepEqual(
			[...third.rows.map((row) => row[1])].sort(),
			[...first.rows.map((row) => row[1])].sort()
		);
		await page.reload({ waitUntil: 'domcontentloaded' });
		await waitForBenchmarkReport(page, 32, 4);
		assert.deepEqual(
			await renderedBenchmarkReport(page),
			third,
			'reload restores the selected report from a fresh response with the same geometry'
		);
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks keeps the latest selection through held replies and retries failed reports', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		publishedSessions = [...sessions];
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);

		const heldB = deferNextApi('/api/v1/benchmark/sessions/session-two');
		await selectBenchmarkSession(page, 'session-two');
		await heldB.started;
		await page.waitForFunction(() => document.querySelector('[data-benchmark-table]') === null, {
			timeout: WAIT_MS
		});
		assert.equal(
			await page.$('[data-benchmark-chart="bars"]'),
			null,
			'old report charts are not shown under a newly selected report'
		);
		await selectBenchmarkSession(page, 'session-three');
		await waitForBenchmarkReport(page, 32, 4);
		heldB.release({ status: 200, body: reports.get('session-two') });
		await page.waitForFunction(
			() =>
				[...(document.querySelectorAll('[data-benchmark-table] tbody tr') ?? [])].some((row) =>
					row.textContent?.includes('alternate-model-1')
				),
			{ timeout: WAIT_MS }
		);
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-three').length,
			1
		);
		assert.deepEqual(
			[...(await renderedBenchmarkReport(page)).rows.map((row) => row[1])].sort(),
			[...sessions[2].modelIds].sort(),
			'a late report response cannot replace the latest selected report'
		);

		apiFailures.reportIds.add('session-four');
		await selectBenchmarkSession(page, 'session-four');
		await waitForText(page, 'That benchmark session could not be loaded.');
		assert.equal(
			await page.$('[data-benchmark-table]'),
			null,
			'failed report does not keep stale charts or rows'
		);
		apiFailures.reportIds.delete('session-four');
		await clickButton(page, 'Retry');
		await waitForBenchmarkReport(page, 40, 3);
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-four').length,
			2,
			'Retry makes one new request for the failed selected report'
		);
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks ignores a held report reply after the confirmed account changes', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		activeAccountFixture = accountFixtures.A;
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.evaluateOnNewDocument(() => {
			let now = Date.now();
			Date.now = () => now;
			window.__advanceClock = (milliseconds) => {
				now += milliseconds;
			};
		});
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);
		assert.equal(requestLog.filter((path) => path === '/api/v1/account').length, 1);

		const heldReport = deferNextApi('/api/v1/benchmark/sessions/session-two');
		await selectBenchmarkSession(page, 'session-two');
		await heldReport.started;

		const ownerProbe = deferNextApi('/api/v1/account');
		activeAccountFixture = accountFixtures.B;
		await page.evaluate(() => {
			window.__advanceClock(31_000);
			window.dispatchEvent(new Event('focus'));
		});
		await ownerProbe.started;
		ownerProbe.release({ status: 200, body: accountFixtures.B });
		await page.waitForFunction(
			() =>
				document.body.innerText.includes('owner-b@example.test') &&
				document.querySelector('[data-benchmark-report]') === null,
			{ timeout: WAIT_MS }
		);
		heldReport.release({ status: 200, body: reports.get('session-two') });
		await page.evaluate(() => new Promise(requestAnimationFrame));
		assert.equal(
			requestLog.filter((path) => path === '/api/v1/benchmark/sessions/session-two').length,
			1,
			'a late response from the prior account does not restart its report request'
		);
		assert.equal(
			completedApiLog.filter((path) => path === '/api/v1/benchmark/sessions/session-two').length,
			0,
			'the canceled prior-owner response is not committed by the fixture server'
		);
		assert.notEqual(
			await page.evaluate(
				() =>
					document
						.querySelector('[data-benchmark-report]')
						?.getAttribute('data-benchmark-report') ?? null
			),
			'session-two',
			'the prior account report does not appear after its response is released'
		);
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks list refresh does not restart a pending selected report detail', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);
		await clickButton(page, 'Images');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === 'images');
		await page.waitForSelector('.grid-scroller, .lineage-viewport', { visible: true });
		const listGate = deferNextApi('/api/v1/benchmark/sessions');
		const detailPath = '/api/v1/benchmark/sessions/session-two';
		const detailGate = deferNextApi(detailPath);
		await clickButton(page, 'Metrics');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === 'metrics');
		await waitForBenchmarkReport(page, 16, 9);
		await waitForActiveViewTransition(page);
		const readyHeight = await page.$eval('[data-benchmark-report]', (root) => root.clientHeight);
		await waitForApiLogCount(requestLog, '/api/v1/benchmark/sessions', 2);
		await listGate.started;
		await selectBenchmarkSession(page, 'session-two');
		await waitForApiLogCount(requestLog, detailPath, 1);
		await detailGate.started;
		listGate.release({
			status: 200,
			body: publishedSessions.map(({ id, created_at }) => ({ id, created_at }))
		});
		await waitForApiLogCount(completedApiLog, '/api/v1/benchmark/sessions', 2);
		await page.evaluate(() => new Promise(requestAnimationFrame));
		const loadingGeometry = await page.evaluate(() => {
			const loading = document.querySelector('[data-benchmark-loading-report="session-two"]');
			return {
				readyRootCount: document.querySelectorAll('[data-benchmark-report]').length,
				loadingMarker: loading !== null,
				loadingHeight: loading?.getBoundingClientRect().height ?? 0,
				minHeight: Number.parseFloat(loading ? getComputedStyle(loading).minHeight : '0'),
				innerHeight: window.innerHeight
			};
		});
		assert.equal(
			loadingGeometry.readyRootCount,
			0,
			'the prior report is removed while B is pending'
		);
		assert.equal(
			loadingGeometry.loadingMarker,
			true,
			'the selected report shows its loading state'
		);
		assert.ok(loadingGeometry.minHeight > 0, 'loading state keeps a positive measured pane height');
		assert.ok(
			loadingGeometry.loadingHeight >= Math.min(readyHeight, loadingGeometry.innerHeight) - 1,
			'loading pane holds the prior report height, capped at the viewport'
		);
		assert.equal(
			requestLog.filter((path) => path === detailPath).length,
			1,
			'the list response does not abort and restart the selected detail request'
		);
		detailGate.release({ status: 200, body: reports.get('session-two') });
		await waitForBenchmarkReport(page, 24, 9);
		assert.equal(requestLog.filter((path) => path === detailPath).length, 1);
		assert.deepEqual(
			(await renderedBenchmarkReport(page)).rows.map((row) => row[1]),
			expectedLeaderboardModelIds(reports.get('session-two')),
			'the held detail response commits after the list refresh'
		);
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Benchmarks commits a cold detail that completes before the refreshed session list', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		await page.setViewport({ width: 1440, height: 1000 });
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics&tab=benchmarks`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForBenchmarkReport(page, 16, 9);
		await clickButton(page, 'Images');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === 'images');
		await page.waitForSelector('.grid-scroller, .lineage-viewport', { visible: true });
		const listGate = deferNextApi('/api/v1/benchmark/sessions');
		const detailPath = '/api/v1/benchmark/sessions/session-two';
		await clickButton(page, 'Metrics');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === 'metrics');
		await waitForText(page, '16 GB VRAM');
		await waitForActiveViewTransition(page);
		await waitForApiLogCount(requestLog, '/api/v1/benchmark/sessions', 2);
		await listGate.started;
		await selectBenchmarkSession(page, 'session-two');
		await waitForApiLogCount(requestLog, detailPath, 1);
		await waitForBenchmarkReport(page, 24, 9);
		const detailSnapshot = await renderedBenchmarkReport(page);
		listGate.release({
			status: 200,
			body: publishedSessions.map(({ id, created_at }) => ({ id, created_at }))
		});
		await waitForApiLogCount(completedApiLog, '/api/v1/benchmark/sessions', 2);
		await page.evaluate(() => new Promise(requestAnimationFrame));
		assert.equal(
			requestLog.filter((path) => path === detailPath).length,
			1,
			'the later list commit keeps the already completed detail body'
		);
		assert.deepEqual(await renderedBenchmarkReport(page), detailSnapshot);
	} finally {
		releasePendingApiGates();
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});

test('Metrics tab navigation skips page transitions but view navigation still transitions', async () => {
	const server = await serveBuild();
	const { port } = server.address();
	let browser;
	try {
		browser = await puppeteer.launch({
			executablePath: await chromeExecutable(),
			headless: true,
			args: ['--no-sandbox', '--disable-setuid-sandbox']
		});
		const page = await browser.newPage();
		const browserLog = watchBrowser(page);
		await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
		await page.evaluateOnNewDocument(() => {
			const nativeTransition = document.startViewTransition?.bind(document);
			window.__transitionCalls = 0;
			window.__transitionSupported = nativeTransition !== undefined;
			if (nativeTransition) {
				document.startViewTransition = (callback) => {
					window.__transitionCalls += 1;
					return nativeTransition(callback);
				};
			}
		});
		await page.goto(`http://127.0.0.1:${port}/app?view=metrics`, {
			waitUntil: 'domcontentloaded'
		});
		await waitForText(page, 'GPU activity');
		assert.equal(await page.evaluate(() => window.__transitionSupported), true);
		assert.equal(new URL(page.url()).searchParams.has('tab'), false);

		await clickButton(page, 'Benchmarks');
		await page.waitForFunction(
			() => new URL(location.href).searchParams.get('tab') === 'benchmarks'
		);
		await waitForText(page, '16 GB VRAM');
		await clickButton(page, 'Usage');
		await page.waitForFunction(() => !new URL(location.href).searchParams.has('tab'));
		await waitForText(page, 'GPU activity');

		await page.goBack();
		await page.waitForFunction(
			() => new URL(location.href).searchParams.get('tab') === 'benchmarks'
		);
		await waitForText(page, '16 GB VRAM');
		await page.goBack();
		await page.waitForFunction(() => !new URL(location.href).searchParams.has('tab'));
		await waitForText(page, 'GPU activity');
		const tabEvidence = await page.evaluate(() => ({
			url: location.href,
			transitionCalls: window.__transitionCalls,
			usageVisible: document.body.innerText.includes('GPU activity'),
			benchmarkVisible: document.body.innerText.includes('GPU vs wall per model')
		}));
		assert.equal(
			tabEvidence.transitionCalls,
			0,
			`Metrics tab changes and Back should skip transitions: ${JSON.stringify(tabEvidence)}`
		);

		await clickButton(page, 'Images');
		await page.waitForFunction(() => new URL(location.href).searchParams.get('view') === 'images');
		await page.waitForSelector('.grid-scroller, .lineage-viewport', { visible: true });
		await page.waitForFunction(() => window.__transitionCalls === 1);
		assert.equal(new URL(page.url()).searchParams.get('view'), 'images');
		assert.equal(await page.evaluate(() => window.__transitionCalls), 1);
		assertBrowserClean(browserLog);
	} finally {
		await browser?.close();
		await new Promise((resolveClose) => server.close(resolveClose));
	}
});
