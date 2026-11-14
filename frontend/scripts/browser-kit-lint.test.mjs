// Guards the lint rule that keeps a static server or a Chrome launch out of the browser checks.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';

const eslint = new ESLint({ cwd: fileURLToPath(new URL('..', import.meta.url)) });

// Each offending call sits on its own line, so a report can be matched to it.
const offending = [
	"import { createServer } from 'node:http';",
	"import puppeteer from 'puppeteer-core';",
	'',
	'export const server = createServer(() => {});',
	'export const browser = await puppeteer.launch({ headless: true });',
	''
].join('\n');

test('a test script that starts its own server or Chrome is reported', async () => {
	const [result] = await eslint.lintText(offending, { filePath: 'scripts/test-fixture.mjs' });
	const lines = result.messages.filter((message) => message.ruleId).map((message) => message.line);
	// The rule may flag the import or the call, so either line counts.
	assert.ok(
		[1, 4].some((line) => lines.includes(line)),
		`createServer not reported: ${JSON.stringify(result.messages)}`
	);
	assert.ok(
		[2, 5].some((line) => lines.includes(line)),
		`puppeteer.launch not reported: ${JSON.stringify(result.messages)}`
	);
});

test('a namespace, alias or https server or Chrome launch is reported too', async () => {
	const aliased = [
		"import * as https from 'node:https';",
		"import { launch } from 'puppeteer-core';",
		'',
		'export const server = https.createServer(() => {});',
		'export const browser = await launch({ headless: true });',
		''
	].join('\n');
	const [result] = await eslint.lintText(aliased, { filePath: 'scripts/test-fixture.mjs' });
	const lines = result.messages.filter((message) => message.ruleId).map((message) => message.line);
	assert.ok(
		lines.includes(4),
		`https.createServer not reported: ${JSON.stringify(result.messages)}`
	);
	assert.ok(lines.includes(2), `named launch not reported: ${JSON.stringify(result.messages)}`);
});

test('browser-kit.mjs may start the server and launch Chrome', async () => {
	const [result] = await eslint.lintText(offending, { filePath: 'scripts/browser-kit.mjs' });
	assert.deepEqual(result.messages, []);
});

test('no scripts/test-*.mjs file starts its own server or Chrome', async () => {
	const results = await eslint.lintFiles(['scripts/test-*.mjs']);
	// Only this rule: npm run lint reports every other rule.
	const kitRules = new Set(['no-restricted-imports', 'no-restricted-syntax']);
	const reported = results.flatMap((result) =>
		result.messages
			.filter((message) => kitRules.has(message.ruleId))
			.map((message) => `${result.filePath}:${message.line} ${message.message}`)
	);
	assert.deepEqual(reported, []);
});
