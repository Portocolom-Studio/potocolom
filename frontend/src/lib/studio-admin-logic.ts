import type { Role } from './account-display';

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
