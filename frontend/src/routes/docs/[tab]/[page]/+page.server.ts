import { error } from '@sveltejs/kit';
import type { EntryGenerator, PageServerLoad } from './$types';
import { docsIndex } from '$lib/docs/source';
import { findPage } from '$lib/docs/model';
import { docPageData } from '$lib/docs/page-data';

export const prerender = true;

export const entries: EntryGenerator = () =>
	docsIndex.pages.map((page) => ({ tab: page.tab, page: page.slug }));

export const load: PageServerLoad = ({ params }) => {
	const page = findPage(docsIndex, params.tab, params.page);
	if (!page) error(404, 'Page not found');
	return docPageData(docsIndex, page);
};
