// Docs model: the tabs, the parsed pages and the sidebar and pager order.
// Pure functions only, so node:test can check them without Vite.

export const DOC_TABS = [
	{ slug: 'guide', label: 'Guide' },
	{ slug: 'self-hosting', label: 'Self-hosting' },
	{ slug: 'api', label: 'API' },
	{ slug: 'architecture', label: 'Architecture' },
	{ slug: 'contributing', label: 'Contributing' },
	{ slug: 'reference', label: 'Reference' }
] as const;

export type DocTab = (typeof DOC_TABS)[number]['slug'];

export type DocPage = {
	tab: DocTab;
	slug: string;
	title: string;
	subtitle: string;
	group: string;
	order: number;
	source: string;
	body: string;
};

export type DocsIndex = {
	pages: DocPage[];
};

export type SidebarGroup = {
	label: string;
	pages: DocPage[];
};

const REQUIRED_KEYS = ['title', 'subtitle', 'tab', 'group', 'order', 'source'] as const;
const PAGE_PATH = /(?:^|\/)pages\/([^/]+)\/([a-z0-9-]+)\.md$/;

function isDocTab(value: string): value is DocTab {
	return DOC_TABS.some((tab) => tab.slug === value);
}

// Splits "---" frontmatter from the body. Each field is one "key: value" line.
export function parseFrontmatter(
	raw: string,
	file: string
): { meta: Record<string, string>; body: string } {
	const text = raw.replace(/\r\n/g, '\n');
	if (!text.startsWith('---\n')) throw new Error(`${file}: missing frontmatter`);
	const end = text.indexOf('\n---\n', 4);
	if (end === -1) throw new Error(`${file}: frontmatter is not closed`);

	const meta: Record<string, string> = {};
	for (const line of text.slice(4, end).split('\n')) {
		if (line.trim() === '') continue;
		const colon = line.indexOf(':');
		if (colon === -1) throw new Error(`${file}: bad frontmatter line "${line}"`);
		const key = line.slice(0, colon).trim();
		const value = line.slice(colon + 1).trim();
		if (!(REQUIRED_KEYS as readonly string[]).includes(key)) {
			throw new Error(`${file}: unknown frontmatter key "${key}"`);
		}
		if (meta[key] !== undefined) throw new Error(`${file}: duplicate key "${key}"`);
		meta[key] = value;
	}
	for (const key of REQUIRED_KEYS) {
		if (!meta[key]) throw new Error(`${file}: missing "${key}"`);
	}
	return { meta, body: text.slice(end + 5).trim() + '\n' };
}

// Builds the sorted index from a map of file path to raw Markdown. Throws on any
// invalid page, so a bad page stops the build instead of shipping a broken link.
export function buildDocsIndex(files: Record<string, string>): DocsIndex {
	const pages: DocPage[] = [];
	const seen = new Set<string>();
	const seenOrder = new Set<string>();

	for (const [file, raw] of Object.entries(files)) {
		const match = PAGE_PATH.exec(file);
		if (!match) throw new Error(`${file}: expected pages/<tab>/<slug>.md`);
		const [, folder, slug] = match;
		const { meta, body } = parseFrontmatter(raw, file);

		if (!isDocTab(folder)) throw new Error(`${file}: unknown folder "${folder}"`);
		if (meta.tab !== folder)
			throw new Error(`${file}: tab "${meta.tab}" must match folder "${folder}"`);

		const order = Number(meta.order);
		if (!Number.isInteger(order) || order < 1)
			throw new Error(`${file}: order must be an integer >= 1`);

		const key = `${folder}/${slug}`;
		if (seen.has(key)) throw new Error(`${file}: duplicate page ${key}`);
		const orderKey = `${folder}#${order}`;
		if (seenOrder.has(orderKey)) throw new Error(`${file}: duplicate order ${order} in ${folder}`);
		seen.add(key);
		seenOrder.add(orderKey);

		pages.push({
			tab: folder,
			slug,
			title: meta.title,
			subtitle: meta.subtitle,
			group: meta.group,
			order,
			source: meta.source,
			body
		});
	}

	const tabRank = new Map(DOC_TABS.map((tab, index) => [tab.slug, index]));
	pages.sort((a, b) => tabRank.get(a.tab)! - tabRank.get(b.tab)! || a.order - b.order);
	return { pages };
}

export function tabPages(index: DocsIndex, tab: DocTab): DocPage[] {
	return index.pages.filter((page) => page.tab === tab);
}

export function findPage(index: DocsIndex, tab: string, slug: string): DocPage | undefined {
	return index.pages.find((page) => page.tab === tab && page.slug === slug);
}

export function docHref(page: Pick<DocPage, 'tab' | 'slug'>): string {
	return `/docs/${page.tab}/${page.slug}`;
}

// Groups appear in the order their first page appears in the tab.
export function sidebarGroups(index: DocsIndex, tab: DocTab): SidebarGroup[] {
	const groups: SidebarGroup[] = [];
	for (const page of tabPages(index, tab)) {
		let group = groups.find((item) => item.label === page.group);
		if (!group) {
			group = { label: page.group, pages: [] };
			groups.push(group);
		}
		group.pages.push(page);
	}
	return groups;
}

export function neighbours(
	index: DocsIndex,
	page: DocPage
): { prev: DocPage | null; next: DocPage | null } {
	const pages = tabPages(index, page.tab);
	const at = pages.indexOf(page);
	return {
		prev: at > 0 ? pages[at - 1] : null,
		next: at >= 0 && at < pages.length - 1 ? pages[at + 1] : null
	};
}
