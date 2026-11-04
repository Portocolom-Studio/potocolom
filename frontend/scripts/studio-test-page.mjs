import { TOUR_DONE_KEY } from '../src/lib/studio-tour.ts';

// Opens a page for the studio checks. The studio starts a first-visit tour that
// takes focus and clicks, so these pages are marked as having seen it before any
// script loads. test-studio-tour.mjs wants the tour and opens its own pages.
export async function newStudioPage(browser) {
	const page = await browser.newPage();
	await page.evaluateOnNewDocument((key) => {
		try {
			localStorage.setItem(key, '1');
		} catch {
			// The initial blank document can have an opaque origin.
		}
	}, TOUR_DONE_KEY);
	return page;
}
