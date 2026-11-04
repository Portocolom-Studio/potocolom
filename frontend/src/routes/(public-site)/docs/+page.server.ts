import type { PageServerLoad } from './$types';
import { findPage } from '$lib/docs/model';
import { docPageData } from '$lib/docs/page-data';
import { docsIndex } from '$lib/docs/source';

// /docs renders the introduction directly (no redirect page, so every built HTML
// file keeps its CSP meta tag). The canonical link points at the real URL.
export const load: PageServerLoad = () => {
	const page = findPage(docsIndex, 'guide', 'introduction');
	if (!page) throw new Error('docs: guide/introduction is missing');
	return docPageData(docsIndex, page);
};
