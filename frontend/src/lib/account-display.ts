import type { MetricsTab, ShellView } from './studio-view';

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

export type GatedSection =
	| 'generate'
	| 'image_to_image'
	| 'upscale'
	| 'edit_image'
	| 'image_to_text'
	| 'realtime_canvas'
	| 'metrics_usage'
	| 'metrics_benchmarks';

const sectionNeededRoles = {
	generate: 'user',
	image_to_image: 'user',
	upscale: 'user',
	edit_image: 'user',
	image_to_text: 'user',
	realtime_canvas: 'user',
	metrics_usage: 'admin',
	metrics_benchmarks: 'admin'
} as const satisfies Record<GatedSection, 'user' | 'admin'>;

// A null role (no account, or AUTH_MODE=none's implicit local admin) marks
// nothing: there is nothing to be locked out of.
export function sectionNeeds(section: GatedSection, role: Role | null): 'user' | 'admin' | null {
	if (role === null) return null;
	if (sectionNeededRoles[section] === 'user') return role === 'viewer' ? 'user' : null;
	return role === 'admin' ? null : 'admin';
}

// A view the role may use stays; otherwise fall back to generate, the main
// panel, and then images, which no role is locked out of.
export function openViewFor(view: ShellView, tab: MetricsTab, role: Role | null): ShellView {
	// models and images are open to every role; metrics picks its section by
	// tab; every other view shares its section's name.
	if (view === 'models' || view === 'images') return view;
	const section: GatedSection =
		view === 'metrics' ? (tab === 'usage' ? 'metrics_usage' : 'metrics_benchmarks') : view;
	if (sectionNeeds(section, role) === null) return view;
	if (sectionNeeds('generate', role) === null) return 'generate';
	return 'images';
}
