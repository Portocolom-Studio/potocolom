import { account } from './account.svelte';
import { parseAccount, type Role } from './account-display';
import { apiFetch } from './api';

export type AuditFilters = {
	actor_user_id: string;
	target_user_id: string;
	action: string;
	limit: string | number;
};

export function needsAdminAttestation(role: Role, mailVerified: boolean): boolean {
	return role === 'admin' && !mailVerified;
}

export function buildAuditQuery(filters: AuditFilters): string {
	const params = new URLSearchParams();
	for (const key of ['actor_user_id', 'target_user_id', 'action'] as const) {
		const value = filters[key].trim();
		if (value !== '') params.set(key, value);
	}
	const limit = String(filters.limit).trim();
	if (limit !== '') params.set('limit', limit);
	return params.toString();
}

export function buildAdminConfirmation(template: string, user: string, change: string): string {
	return template.replaceAll('{user}', user).replaceAll('{change}', change);
}

const USERS_QUERY_LIMIT = 50;
const USERS_SEARCH_MAX_LENGTH = 200;

// Mirrors GET /api/v1/users: a trimmed q (capped the same way the backend
// caps it, so a client-side truncation and the server's agree) and the
// cursor from the previous page's next_cursor, or none for the first page.
export function buildUsersQuery(q: string, cursor: string | null): string {
	const params = new URLSearchParams();
	params.set('limit', String(USERS_QUERY_LIMIT));
	const trimmed = q.trim().slice(0, USERS_SEARCH_MAX_LENGTH);
	if (trimmed !== '') params.set('q', trimmed);
	if (cursor !== null) params.set('cursor', cursor);
	return `?${params.toString()}`;
}

// FastAPI's own validation errors are an array of {msg, ...} rather than a
// string; the first item covers the common case of one bad query parameter.
export function adminErrorMessage(detail: unknown, fallback: string): string {
	if (typeof detail === 'string') return detail;
	if (Array.isArray(detail) && detail.length > 0) {
		const first = detail[0] as { msg?: unknown } | undefined;
		if (first && typeof first.msg === 'string') return first.msg;
	}
	return fallback;
}

// Mirrors POST /api/v1/invitations: a trimmed address, so a pasted link's
// surrounding whitespace does not become part of the invited email.
export function inviteRequestBody(email: string, role: Role): { email: string; role: Role } {
	return { email: email.trim(), role };
}

// The 403 this route answers when inviting an administrator without a recent
// sign-in. Matched by value, the same way auth-flow.ts reads other details,
// so the studio can show a hint that is specific to this one cause.
export function isRecentAuthenticationRequired(detail: unknown): boolean {
	return detail === 'recent authentication required';
}

// Called on a 403 from any admin route: an account that was demoted while
// the view stayed open gets the same re-read the studio does at startup, so
// its account.current (and the openViewFor fallback that watches it from
// +page.svelte) catches up with the account's current role.
export async function recheckAdminAccess(): Promise<void> {
	try {
		const response = await apiFetch('/api/v1/account');
		// Only a 401 means signed out; a passing server error must not sign an
		// administrator out of the studio.
		if (response.ok) account.current = parseAccount(await response.json());
		else if (response.status === 401) account.current = null;
	} catch {
		// Best effort: a failed recheck leaves the stale account in place,
		// and the next privileged action retries it.
	}
}
