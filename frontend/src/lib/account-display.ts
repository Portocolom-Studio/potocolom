export type Role = 'admin' | 'user' | 'viewer';

const roleLabelKeys = {
	admin: 'app.shell.role_admin',
	user: 'app.shell.role_user',
	viewer: 'app.shell.role_viewer'
} as const satisfies Record<Role, string>;

export function parseAccount(body: unknown): { email: string; role: Role } | null {
	if (typeof body !== 'object' || body === null) return null;
	const { email, role } = body as { email?: unknown; role?: unknown };
	if (typeof email !== 'string' || email === '') return null;
	if (typeof role !== 'string' || !Object.hasOwn(roleLabelKeys, role)) return null;
	return { email, role: role as Role };
}

export function accountInitial(email: string): string {
	return email.trim().charAt(0).toUpperCase();
}

export function accountRoleLabelKey(role: Role): (typeof roleLabelKeys)[Role] {
	return roleLabelKeys[role];
}
