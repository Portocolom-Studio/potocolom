import type { Role } from './account-display';

export type AuditFilters = {
	actor_user_id: string;
	target_user_id: string;
	action: string;
	limit: string | number;
};

export type ModelWorkerAssignment = {
	id: string;
	name: string;
	workerIds: string[];
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

export function modelWorkerAssignments(body: unknown): ModelWorkerAssignment[] {
	if (!Array.isArray(body)) return [];
	return body.flatMap((item): ModelWorkerAssignment[] => {
		if (typeof item !== 'object' || item === null || Array.isArray(item)) return [];
		const model = item as { id?: unknown; name?: unknown; worker_ids?: unknown };
		if (
			typeof model.id !== 'string' ||
			typeof model.name !== 'string' ||
			!Array.isArray(model.worker_ids)
		) {
			return [];
		}
		const workerIds = model.worker_ids.filter(
			(worker): worker is string => typeof worker === 'string'
		);
		return workerIds.length > 0 ? [{ id: model.id, name: model.name, workerIds }] : [];
	});
}
