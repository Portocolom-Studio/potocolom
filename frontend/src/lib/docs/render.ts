// Markdown to HTML for the docs pages. Runs at build time only (the page server
// load), so marked never ships to the browser. Output is plain HTML: no inline
// scripts, which keeps the hash-mode CSP unchanged.

import { marked } from 'marked';
import { iconSvg } from './icons';

export type TocEntry = { id: string; text: string };

const CALLOUT_LABELS: Record<string, string> = {
	NOTE: 'Note',
	WARNING: 'Warning',
	PLANNED: 'Planned'
};

const ENTITIES: Record<string, string> = {
	'&amp;': '&',
	'&lt;': '<',
	'&gt;': '>',
	'&quot;': '"',
	'&#39;': "'"
};

function plainText(html: string): string {
	return html
		.replace(/<[^>]+>/g, '')
		.replace(/&(amp|lt|gt|quot|#39);/g, (entity) => ENTITIES[entity])
		.trim();
}

export function slugify(text: string): string {
	return text
		.normalize('NFKD')
		.replace(/[\u0080-\uffff]/g, '')
		.toLowerCase()
		.replace(/[^a-z0-9\s-]/g, '')
		.trim()
		.replace(/\s+/g, '-');
}

// Adds an id to every H2 and H3, and returns the H2s for "On this page".
function addHeadingIds(html: string): { html: string; toc: TocEntry[] } {
	const used = new Map<string, number>();
	const toc: TocEntry[] = [];
	const out = html.replace(
		/<h([23])>([\s\S]*?)<\/h\1>/g,
		(_match, level: string, inner: string) => {
			const text = plainText(inner);
			const base = slugify(text) || 'section';
			const count = used.get(base) ?? 0;
			used.set(base, count + 1);
			const id = count === 0 ? base : `${base}-${count + 1}`;
			if (level === '2') toc.push({ id, text });
			return `<h${level} id="${id}">${inner}</h${level}>`;
		}
	);
	return { html: out, toc };
}

// A blockquote whose first line is [!NOTE], [!WARNING] or [!PLANNED] becomes a callout.
function addCallouts(html: string): string {
	return html.replace(/<blockquote>([\s\S]*?)<\/blockquote>/g, (match, inner: string) => {
		const kind = /^\s*<p>\[!(NOTE|WARNING|PLANNED)\]\s*/.exec(inner);
		if (!kind) return match;
		const afterLabel = inner.slice(kind[0].length);
		// A blank line after the marker makes marked close the paragraph; keep the next paragraph, drop the empty one.
		const body = afterLabel.startsWith('</p>') ? afterLabel.slice(4) : `<p>${afterLabel}`;
		const name = kind[1];
		return (
			`<aside class="callout callout-${name.toLowerCase()}">` +
			`<span class="callout-label">${CALLOUT_LABELS[name]}</span>` +
			body +
			'</aside>'
		);
	});
}

// Wraps each code block with a label and a copy button. The copy handler lives
// in the page component (event delegation), so no inline script is needed.
function wrapCodeBlocks(html: string): string {
	return html.replace(
		/<pre><code([^>]*)>([\s\S]*?)<\/code><\/pre>/g,
		(_match, attrs: string, code: string) => {
			const lang = /class="language-([\w-]+)"/.exec(attrs)?.[1] ?? 'text';
			return (
				`<div class="codeblock">` +
				`<div class="codeblock-bar"><span>${lang}</span>` +
				`<button type="button" class="copy-code">Copy</button></div>` +
				`<pre><code${attrs}>${code}</code></pre>` +
				`</div>`
			);
		}
	);
}

// Card icons: <span class="docs-card-icon" data-icon="name"></span> becomes an SVG.
function addCardIcons(html: string): string {
	return html.replace(
		/<span class="docs-card-icon" data-icon="([a-z-]+)"><\/span>/g,
		(_match, name: string) => iconSvg(name)
	);
}

export function renderBody(markdown: string): { html: string; toc: TocEntry[] } {
	const raw = marked.parse(markdown, { async: false, gfm: true }) as string;
	const withIds = addHeadingIds(raw);
	return {
		html: wrapCodeBlocks(addCallouts(addCardIcons(withIds.html))).replace(
			/<table>[\s\S]*?<\/table>/g,
			(table) =>
				`<div class="table-scroll" role="region" aria-label="Scrollable table" tabindex="0">${table}</div>`
		),
		toc: withIds.toc
	};
}
