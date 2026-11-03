import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderBody, slugify } from './render.ts';

test('turns a NOTE blockquote into a note callout', () => {
	const { html } = renderBody(['> [!NOTE]', '> Pre-alpha software.'].join('\n'));
	assert.match(
		html,
		/<aside class="callout callout-note"><span class="callout-label">Note<\/span><p>Pre-alpha software\.<\/p>\s*<\/aside>/
	);
	assert.doesNotMatch(html, /<blockquote>/);
});

test('turns PLANNED and WARNING blockquotes into callouts of their kind', () => {
	const { html } = renderBody(
		['> [!PLANNED]', '> Designed only.', '', '> [!WARNING]', '> Back up first.'].join('\n')
	);
	assert.match(html, /callout-planned"><span class="callout-label">Planned<\/span>/);
	assert.match(html, /callout-warning"><span class="callout-label">Warning<\/span>/);
});

test('leaves an ordinary blockquote alone', () => {
	const { html } = renderBody('> Just a quote.');
	assert.match(html, /<blockquote>/);
	assert.doesNotMatch(html, /callout/);
});

test('gives H2 and H3 headings ids and lists only H2 in the table of contents', () => {
	const { html, toc } = renderBody(['## Install', '', '### Linux', '', '## Upgrade'].join('\n'));
	assert.match(html, /<h2 id="install">Install<\/h2>/);
	assert.match(html, /<h3 id="linux">Linux<\/h3>/);
	assert.deepEqual(toc, [
		{ id: 'install', text: 'Install' },
		{ id: 'upgrade', text: 'Upgrade' }
	]);
});

test('makes duplicate heading ids unique', () => {
	const { html } = renderBody(['## Notes', '', '## Notes'].join('\n'));
	assert.match(html, /<h2 id="notes">Notes<\/h2>/);
	assert.match(html, /<h2 id="notes-2">Notes<\/h2>/);
});

test('wraps a code block with its language and a copy button', () => {
	const { html } = renderBody(['```bash', 'make verify', '```'].join('\n'));
	assert.match(html, /<div class="codeblock"><div class="codeblock-bar"><span>bash<\/span>/);
	assert.match(html, /<button type="button" class="copy-code">Copy<\/button>/);
	assert.match(html, /<pre><code class="language-bash">make verify\n<\/code><\/pre>/);
});

test('replaces a card icon marker with an SVG', () => {
	const { html } = renderBody('<span class="docs-card-icon" data-icon="rocket"></span>');
	assert.match(html, /<svg class="docs-icon" viewBox="0 0 24 24"/);
	assert.doesNotMatch(html, /data-icon/);
});

test('fails the build on an unknown card icon', () => {
	assert.throws(
		() => renderBody('<span class="docs-card-icon" data-icon="nope"></span>'),
		/unknown icon "nope"/
	);
});

test('a callout with a blank line after its marker has no empty paragraph', () => {
	const { html } = renderBody(['> [!NOTE]', '>', '> Text here.'].join('\n'));
	assert.doesNotMatch(html, /<p><\/p>/);
	assert.match(html, /<p>Text here\.<\/p>/);
	assert.match(html, /callout-note/);
});

test('keeps code and entities out of table of contents text', () => {
	const { toc } = renderBody(['## Use `make` & test'].join('\n'));
	assert.deepEqual(toc, [{ id: 'use-make-test', text: 'Use make & test' }]);
});

test('slugify keeps ASCII letters, digits and hyphens', () => {
	assert.equal(slugify('Accounts and the root key'), 'accounts-and-the-root-key');
	assert.equal(slugify('TLS & HSTS'), 'tls-hsts');
});
