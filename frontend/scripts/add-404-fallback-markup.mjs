#!/usr/bin/env node
/**
 * Post-build step: give build/404.html a title, a heading and a home link.
 *
 * The adapter-static fallback is the SPA shell with an empty app root, so a
 * missing URL showed nothing with JavaScript off. The markup goes in a
 * <noscript> block: browsers parse it as elements only when scripts are off,
 * so the mounted error page never shows a second heading. The text reuses the
 * error page's i18n keys, so both pages say the same thing.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const file = join(root, 'build', '404.html');

const messages = (locale) =>
	JSON.parse(readFileSync(join(root, 'src', 'lib', 'i18n', `${locale}.json`), 'utf8'));
const en = messages('en');
const es = messages('es');
const escape = (text) =>
	text.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const bilingual = (key) =>
	`<span lang="en">${escape(en[key])}</span> / <span lang="es">${escape(es[key])}</span>`;

const appRoot = '<div style="display: contents">';
const headEnd = '</head>';
const markup =
	'<noscript><main class="grid min-h-screen place-items-center px-6 text-center">' +
	'<div class="flex flex-col items-center gap-3">' +
	`<h1 class="text-2xl font-semibold tracking-tight">${bilingual('error.title404')}</h1>` +
	`<a class="text-foreground underline underline-offset-4" href="/">${bilingual('error.home')}</a>` +
	'</div></main></noscript>';

const html = readFileSync(file, 'utf8');
if (!html.includes(appRoot) || !html.includes(headEnd)) {
	throw new Error(`${file}: no app root or head to write the fallback markup into`);
}

const titled = html.replace(
	headEnd,
	() => `\t\t<title>potocolom - ${escape(en['error.title404'])}</title>\n\t${headEnd}`
);
writeFileSync(
	file,
	titled.replace(appRoot, () => appRoot + markup)
);
