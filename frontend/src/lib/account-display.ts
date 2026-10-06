import type { StudioTab, ShellView } from './studio-view';

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
	| 'metrics_benchmarks'
	| 'metrics_mine'
	| 'admin';

const sectionNeededRoles = {
	generate: 'user',
	image_to_image: 'user',
	upscale: 'user',
	edit_image: 'user',
	image_to_text: 'user',
	realtime_canvas: 'user',
	metrics_usage: 'admin',
	metrics_benchmarks: 'admin',
	metrics_mine: 'user',
	admin: 'admin'
} as const satisfies Record<GatedSection, 'user' | 'admin'>;

// A null role (no account, or AUTH_MODE=none's implicit local admin) marks
// nothing: there is nothing to be locked out of. The one exception is the
// admin view, whose user and audit API exists only in accounts mode.
export function sectionNeeds(section: GatedSection, role: Role | null): 'user' | 'admin' | null {
	if (role === null) return section === 'admin' ? 'admin' : null;
	if (sectionNeededRoles[section] === 'user') return role === 'viewer' ? 'user' : null;
	return role === 'admin' ? null : 'admin';
}

function metricsSection(tab: StudioTab): GatedSection {
	if (tab === 'benchmarks') return 'metrics_benchmarks';
	if (tab === 'mine') return 'metrics_mine';
	return 'metrics_usage';
}

// Every refusal lands here: generate first, and images for the one role
// locked out of generate.
function refusedView(role: Role | null): ShellView {
	return sectionNeeds('generate', role) === null ? 'generate' : 'images';
}

// A view the role may use stays; otherwise fall back to generate, the main
// panel, and then images, which no role is locked out of.
export function openViewFor(view: ShellView, tab: StudioTab, role: Role | null): ShellView {
	// models and images are open to every role; metrics picks its section by
	// tab; admin and every other view share their section's name.
	if (view === 'models' || view === 'images') return view;
	// The account view needs a signed-in account: AUTH_MODE=none has a null
	// role and no account to show, so it is refused with everything else.
	if (view === 'account') return role === null ? refusedView(role) : view;
	const section: GatedSection = view === 'metrics' ? metricsSection(tab) : view;
	if (sectionNeeds(section, role) === null) return view;
	return refusedView(role);
}
