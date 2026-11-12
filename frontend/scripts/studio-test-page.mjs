import { TOUR_DONE_KEY } from '../src/lib/studio-tour.ts';

// Every browser check reports the browser side of a failing run. The events of all
// pages in the process are kept here (newest last, at most MAX_PAGE_EVENTS lines) and
// printed to stderr when the process exits with a non-zero code, which node:test and an
// uncaught error both set. A check that fails under load reads differently when a
// request, a script or the theme script went wrong first.
const MAX_PAGE_EVENTS = 1000;
const pageEvents = [];

function pageLabel(page, label) {
	if (label) return label;
	try {
		const url = new URL(page.url());
		return url.protocol.startsWith('http') ? url.pathname : 'page';
	} catch {
		return 'page';
	}
}

// Attaches the listeners to one page. The label defaults to the page's URL path.
export function recordPageEvents(page, label) {
	const note = (text) => {
		pageEvents.push(`${pageLabel(page, label)}: ${text}`);
		if (pageEvents.length > MAX_PAGE_EVENTS)
			pageEvents.splice(0, pageEvents.length - MAX_PAGE_EVENTS);
	};
	page.on('pageerror', (error) => note(`pageerror ${error.message}`));
	page.on('console', (message) => {
		if (message.type() === 'error' || message.type() === 'warn')
			note(`console.${message.type()} ${message.text()}`);
	});
	page.on('requestfailed', (request) =>
		note(`requestfailed ${request.url()} ${request.failure()?.errorText}`)
	);
	page.on('response', (response) => {
		if (response.status() >= 400) note(`HTTP ${response.status()} ${response.url()}`);
	});
}

// Prints the held events and empties the list, so a check that prints them itself
// does not print them again at exit.
export function printPageEvents() {
	if (pageEvents.length === 0) return;
	console.error(`browser page events (${pageEvents.length}):`);
	for (const line of pageEvents) console.error(`  ${line}`);
	pageEvents.length = 0;
}

process.on('exit', () => {
	if (process.exitCode) printPageEvents();
});

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
	recordPageEvents(page);
	return page;
}
