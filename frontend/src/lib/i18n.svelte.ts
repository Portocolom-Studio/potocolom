import { MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, passwordLengthError } from './account-logic';
// Hand-rolled i18n: two flat dictionaries behind a tiny reactive store
// (docs/decisions.md, "Frontend foundation"). Every user-facing string goes
// through t() from the first component onward.
import { readStored, writeStored } from './safe-storage';
import en from './i18n/en.json';
import es from './i18n/es.json';

const dictionaries = { en, es } as const;

export type Locale = keyof typeof dictionaries;
export const locales = Object.keys(dictionaries) as Locale[];

function preferredLocale(): Locale {
	const saved = readStored('locale');
	if (saved && saved in dictionaries) return saved as Locale;
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
	writeStored('locale', locale);
	document.documentElement.lang = locale;
}

export function t(key: keyof typeof en): string {
	return dictionaries[state.locale][key] ?? dictionaries.en[key] ?? key;
}

export function formatDateTime(value: string | null): string {
	if (!value) return '-';
	const date = Date.parse(value);
	if (!Number.isFinite(date)) return value;
	return new Intl.DateTimeFormat(getLocale(), { dateStyle: 'medium', timeStyle: 'short' }).format(
		date
	);
}

export function passwordHint(): string {
	return t('auth.password.hint')
		.replace('{min}', String(MIN_PASSWORD_LENGTH))
		.replace('{max}', String(MAX_PASSWORD_LENGTH));
}

export function passwordLengthMessage(password: string): string {
	const issue = passwordLengthError(password);
	if (!issue) return '';
	return t(issue.kind === 'short' ? 'auth.password.too_short' : 'auth.password.too_long')
		.replace('{count}', String(issue.count))
		.replace('{limit}', String(issue.limit));
}
