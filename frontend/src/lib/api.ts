const CSRF_COOKIE_NAMES = ['__Host-potocolom_csrf', 'potocolom_csrf'] as const;

export function readCsrfToken(cookie: string): string | null {
	for (const name of CSRF_COOKIE_NAMES) {
		const match = cookie.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
		if (match?.[1]) return decodeURIComponent(match[1]);
	}
	return null;
}

export function csrfHeaders(cookie: string): Record<string, string> {
	const token = readCsrfToken(cookie);
	return token ? { 'x-csrf-token': token } : {};
}

export async function apiFetch(
	input: RequestInfo | URL,
	init: RequestInit = {}
): Promise<Response> {
	const headers = new Headers(init.headers);
	if (typeof document !== 'undefined') {
		const csrf = csrfHeaders(document.cookie);
		for (const [key, value] of Object.entries(csrf)) {
			headers.set(key, value);
		}
	}
	return fetch(input, { ...init, credentials: 'include', headers });
}
