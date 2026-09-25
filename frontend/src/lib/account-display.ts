export type Role = 'admin' | 'user' | 'viewer';

const roleLabelKeys = {
	admin: 'app.shell.role_admin',
	user: 'app.shell.role_user',
	viewer: 'app.shell.role_viewer'
} as const satisfies Record<Role, string>;

export function accountInitial(email: string): string {
	return email.trim().charAt(0).toUpperCase();
}

export function accountRoleLabelKey(role: Role): (typeof roleLabelKeys)[Role] {
	return roleLabelKeys[role];
}
