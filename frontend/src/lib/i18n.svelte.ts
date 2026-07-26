// Hand-rolled i18n: two flat dictionaries behind a tiny reactive store
// (docs/decisions.md, "Frontend foundation"). Every user-facing string goes
// through t() from the first component onward.
import en from './i18n/en.json';
import es from './i18n/es.json';

const dictionaries = { en, es } as const;

export type Locale = keyof typeof dictionaries;
export const locales = Object.keys(dictionaries) as Locale[];

function preferredLocale(): Locale {
	if (typeof localStorage === 'undefined') return 'en';
	try {
		const saved = localStorage.getItem('locale');
		if (saved && saved in dictionaries) return saved as Locale;
	} catch {
		// Storage access throws where the browser blocks it, so fall back to the
		// browser language rather than the stored one (as studio.svelte.ts does).
	}
	return navigator.language.startsWith('es') ? 'es' : 'en';
}

// English is the prerendered language. Restore the browser preference only
// after hydration so the server and initial client render stay identical.
const state = $state<{ locale: Locale }>({ locale: 'en' });

export function initializeLocale(): void {
	const locale = preferredLocale();
	state.locale = locale;
	document.documentElement.lang = locale;
}

export function getLocale(): Locale {
	return state.locale;
}

export function setLocale(locale: Locale): void {
	state.locale = locale;
	localStorage.setItem('locale', locale);
	document.documentElement.lang = locale;
}

export function t(key: keyof typeof en): string {
	return dictionaries[state.locale][key] ?? dictionaries.en[key] ?? key;
}
