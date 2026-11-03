import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
	buildDocsIndex,
	docHref,
	findPage,
	neighbours,
	parseFrontmatter,
	sidebarGroups,
	tabPages
} from './model.ts';

function page(fields: Record<string, string>, body = 'Body text.'): string {
	const defaults: Record<string, string> = {
		title: 'Quickstart',
		subtitle: 'Install and open the studio.',
		tab: 'guide',
		group: 'Get started',
		order: '2',
		source: 'README.md "Self-hosting"'
	};
	const merged = { ...defaults, ...fields };
	const lines = Object.entries(merged).map(([key, value]) => `${key}: ${value}`);
	return ['---', ...lines, '---', '', body, ''].join('\n');
}

test('parses frontmatter and returns the body', () => {
	const { meta, body } = parseFrontmatter(page({}, 'Hello.'), 'x.md');
	assert.equal(meta.title, 'Quickstart');
	assert.equal(meta.order, '2');
	assert.equal(body, 'Hello.\n');
});

test('rejects a page with no frontmatter', () => {
	assert.throws(() => parseFrontmatter('# Title\n', 'x.md'), /missing frontmatter/);
});

test('rejects an unknown frontmatter key', () => {
	assert.throws(
		() => parseFrontmatter(page({ colour: 'blue' }), 'x.md'),
		/unknown frontmatter key "colour"/
	);
});

test('rejects a missing required key', () => {
	const raw = page({}).replace('source: README.md "Self-hosting"\n', '');
	assert.throws(() => parseFrontmatter(raw, 'x.md'), /missing "source"/);
});

test('rejects a tab that does not match its folder', () => {
	const files = {
		'./pages/api/quickstart.md': page({ tab: 'guide' })
	};
	assert.throws(() => buildDocsIndex(files), /must match folder "api"/);
});

test('rejects an unknown tab folder', () => {
	const files = { './pages/nope/quickstart.md': page({ tab: 'nope' }) };
	assert.throws(() => buildDocsIndex(files), /unknown folder "nope"/);
});

test('rejects two pages with the same order in one tab', () => {
	const files = {
		'./pages/guide/a.md': page({ order: '1' }),
		'./pages/guide/b.md': page({ order: '1' })
	};
	assert.throws(() => buildDocsIndex(files), /duplicate order 1 in guide/);
});

test('rejects a non-integer order', () => {
	const files = { './pages/guide/a.md': page({ order: '1.5' }) };
	assert.throws(() => buildDocsIndex(files), /order must be an integer/);
});

test('rejects a file name that is not a lowercase slug', () => {
	const files = { './pages/guide/Quick_Start.md': page({}) };
	assert.throws(() => buildDocsIndex(files), /expected pages\/<tab>\/<slug>\.md/);
});

test('sorts pages by tab order, then page order', () => {
	const index = buildDocsIndex({
		'./pages/api/overview.md': page({ tab: 'api', order: '1', title: 'API overview' }),
		'./pages/guide/quickstart.md': page({ order: '2' }),
		'./pages/guide/introduction.md': page({ order: '1', title: 'Introduction' })
	});
	assert.deepEqual(
		index.pages.map((p) => `${p.tab}/${p.slug}`),
		['guide/introduction', 'guide/quickstart', 'api/overview']
	);
});

test('groups keep the order of their first page', () => {
	const index = buildDocsIndex({
		'./pages/guide/a.md': page({ order: '1', group: 'Start' }),
		'./pages/guide/b.md': page({ order: '2', group: 'Later' }),
		'./pages/guide/c.md': page({ order: '3', group: 'Start' })
	});
	const groups = sidebarGroups(index, 'guide');
	assert.deepEqual(
		groups.map((g) => [g.label, g.pages.map((p) => p.slug)]),
		[
			['Start', ['a', 'c']],
			['Later', ['b']]
		]
	);
});

test('previous and next stay inside the tab', () => {
	const index = buildDocsIndex({
		'./pages/guide/a.md': page({ order: '1' }),
		'./pages/guide/b.md': page({ order: '2' }),
		'./pages/api/x.md': page({ tab: 'api', order: '1' })
	});
	const [a, b] = tabPages(index, 'guide');
	assert.equal(neighbours(index, a).prev, null);
	assert.equal(neighbours(index, a).next, b);
	assert.equal(neighbours(index, b).next, null);
	assert.equal(neighbours(index, b).prev, a);
});

test('rejects a duplicate frontmatter key', () => {
	const raw =
		'---\ntitle: A\ntitle: B\nsubtitle: s\ntab: guide\ngroup: g\norder: 1\nsource: x\n---\n\nBody\n';
	assert.throws(() => parseFrontmatter(raw, 'x.md'), /duplicate key "title"/);
});

test('rejects frontmatter that is never closed', () => {
	assert.throws(() => parseFrontmatter('---\ntitle: A\n', 'x.md'), /not closed/);
});

test('accepts CRLF line endings', () => {
	const { meta } = parseFrontmatter(page({}).replace(/\n/g, '\r\n'), 'x.md');
	assert.equal(meta.title, 'Quickstart');
});

test('findPage and docHref agree on the URL shape', () => {
	const index = buildDocsIndex({
		'./pages/self-hosting/gpu-passthrough.md': page({ tab: 'self-hosting', order: '1' })
	});
	const found = findPage(index, 'self-hosting', 'gpu-passthrough');
	assert.ok(found);
	assert.equal(docHref(found), '/docs/self-hosting/gpu-passthrough');
	assert.equal(findPage(index, 'self-hosting', 'missing'), undefined);
});
