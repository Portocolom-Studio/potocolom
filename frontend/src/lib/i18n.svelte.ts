// Hand-rolled i18n: two flat dictionaries behind a tiny reactive store
// (docs/decisions.md, "Frontend foundation"). Every user-facing string goes
// through t() from the first component onward.
import en from './i18n/en.json';
import es from './i18n/es.json';

const dictionaries = { en, es } as const;

export type Locale = keyof typeof dictionaries;
export const locales = Object.keys(dictionaries) as Locale[];

function initialLocale(): Locale {
	if (typeof localStorage === 'undefined') return 'en';
	const saved = localStorage.getItem('locale');
	if (saved && saved in dictionaries) return saved as Locale;
	return navigator.language.startsWith('es') ? 'es' : 'en';
}

const state = $state({ locale: initialLocale() });
if (typeof document !== 'undefined') document.documentElement.lang = state.locale;

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
