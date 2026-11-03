// The data a docs page needs, shared by /docs/[tab]/[page] and /docs. Server only
// (it renders Markdown), so it is imported from +page.server.ts files alone.
import {
	DOC_TABS,
	docHref,
	neighbours,
	sidebarGroups,
	tabPages,
	type DocPage,
	type DocsIndex
} from './model';
import { pageIcon } from './icons';
import { renderBody } from './render';

export const SITE_URL = 'https://potocolom.leonfuller.com';

function tabLabel(tab: string): string {
	return DOC_TABS.find((item) => item.slug === tab)?.label ?? tab;
}

export function docPageData(index: DocsIndex, page: DocPage) {
	const { html, toc } = renderBody(page.body);
	const { prev, next } = neighbours(index, page);

	return {
		page: {
			tab: page.tab,
			slug: page.slug,
			title: page.title,
			subtitle: page.subtitle,
			group: page.group
		},
		canonical: `${SITE_URL}${docHref(page)}`,
		html,
		toc,
		tabs: DOC_TABS.filter((tab) => tabPages(index, tab.slug).length > 0).map((tab) => ({
			slug: tab.slug,
			label: tab.label,
			href: docHref(tabPages(index, tab.slug)[0])
		})),
		groups: sidebarGroups(index, page.tab).map((group) => ({
			label: group.label,
			pages: group.pages.map((item) => ({
				slug: item.slug,
				title: item.title,
				href: docHref(item),
				icon: pageIcon(item.tab, item.slug),
				current: item.slug === page.slug
			}))
		})),
		// Every page, for the sidebar search. Filtered in the browser.
		searchIndex: index.pages.map((item) => ({
			title: item.title,
			subtitle: item.subtitle,
			tabLabel: tabLabel(item.tab),
			href: docHref(item),
			icon: pageIcon(item.tab, item.slug)
		})),
		prev: prev && { title: prev.title, href: docHref(prev) },
		next: next && { title: next.title, href: docHref(next) }
	};
}
