import type { StudioTab, ShellView } from './studio-view';

export type Role = 'admin' | 'user' | 'viewer';

export type Capability = 'generate' | 'edit_own_work' | 'view_own_usage' | 'administer';

const roleLabelKeys = {
	admin: 'app.shell.role_admin',
	user: 'app.shell.role_user',
	viewer: 'app.shell.role_viewer'
} as const satisfies Record<Role, string>;

const capabilityNames = new Set<string>([
	'generate',
	'edit_own_work',
	'view_own_usage',
	'administer'
]);

const legacyRoleCapabilities = {
	viewer: [],
	user: ['generate', 'edit_own_work', 'view_own_usage'],
	admin: ['generate', 'edit_own_work', 'view_own_usage', 'administer']
} as const satisfies Record<Role, readonly Capability[]>;

export type ParsedAccount = {
	email: string;
	role: string;
	capabilities: ReadonlySet<Capability>;
};

export function isKnownRole(role: string): role is Role {
	return Object.hasOwn(roleLabelKeys, role);
}

function usableRole(role: unknown): role is string {
	return typeof role === 'string' && role !== '';
}

function capabilitiesFrom(body: object, role: string): ReadonlySet<Capability> {
	if (!Object.hasOwn(body, 'capabilities')) {
		return isKnownRole(role) ? new Set(legacyRoleCapabilities[role]) : new Set();
	}
	const value = (body as { capabilities?: unknown }).capabilities;
	if (!Array.isArray(value)) return new Set();
	const kept = new Set<Capability>();
	for (const item of value) {
		if (typeof item === 'string' && capabilityNames.has(item)) kept.add(item as Capability);
	}
	return kept;
}

export function parseAccount(body: unknown): ParsedAccount | null {
	if (typeof body !== 'object' || body === null) return null;
	const { email, role } = body as { email?: unknown; role?: unknown };
	if (typeof email !== 'string' || email === '') return null;
	if (!usableRole(role)) return null;
	return { email, role, capabilities: capabilitiesFrom(body, role) };
}

/** Return a stable account identity without changing display/role validation. */
export function accountOwnerId(body: unknown): string | null {
	if (typeof body !== 'object' || body === null) return null;
	const id = (body as { id?: unknown }).id;
	return typeof id === 'string' &&
		/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id)
		? id.toLowerCase()
		: null;
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

const sectionCapability = {
	generate: 'generate',
	image_to_image: 'generate',
	upscale: 'generate',
	edit_image: 'generate',
	image_to_text: 'generate',
	realtime_canvas: 'generate',
	metrics_usage: 'administer',
	metrics_benchmarks: 'administer',
	metrics_mine: 'view_own_usage',
	admin: 'administer'
} as const satisfies Record<GatedSection, Capability>;

// Null is no account object: confirmed local mode, or a probe that produced
// none. An unknown role is an account with a set, including an empty one.
export function presentedCapabilities(
	current: { capabilities: ReadonlySet<Capability> } | null,
	localModeConfirmed: boolean
): ReadonlySet<Capability> | null {
	if (localModeConfirmed || current === null) return null;
	return current.capabilities;
}

// The install GPU poll. A missing account still polls, which is local mode.
// An account polls only when it holds administer.
export function mayReadInstallGpu(
	current: { capabilities: ReadonlySet<Capability> } | null,
	localModeConfirmed: boolean
): boolean {
	const capabilities = presentedCapabilities(current, localModeConfirmed);
	return capabilities === null || capabilities.has('administer');
}

// Model install and the setup card. Local mode qualifies. A missing account
// that is not confirmed local mode does not.
export function mayAdministerAccounts(
	current: { capabilities: ReadonlySet<Capability> } | null,
	localModeConfirmed: boolean
): boolean {
	if (localModeConfirmed) return true;
	return current?.capabilities.has('administer') ?? false;
}

// A null set (no account) marks nothing, except the admin view, whose user
// and audit API exists only in accounts mode.
export function sectionNeeds(
	section: GatedSection,
	capabilities: ReadonlySet<Capability> | null
): 'user' | 'admin' | null {
	if (capabilities === null) return section === 'admin' ? 'admin' : null;
	const needed = sectionCapability[section];
	if (capabilities.has(needed)) return null;
	return needed === 'administer' ? 'admin' : 'user';
}

function metricsSection(tab: StudioTab): GatedSection {
	if (tab === 'benchmarks') return 'metrics_benchmarks';
	if (tab === 'mine') return 'metrics_mine';
	return 'metrics_usage';
}

// Every refusal lands here: generate first, and images for an account locked
// out of generate.
function refusedView(capabilities: ReadonlySet<Capability> | null): ShellView {
	return sectionNeeds('generate', capabilities) === null ? 'generate' : 'images';
}

// A view the capabilities may use stays; otherwise fall back to generate, the
// main panel, and then images, which no account is locked out of.
export function openViewFor(
	view: ShellView,
	tab: StudioTab,
	capabilities: ReadonlySet<Capability> | null,
	localMode = false
): ShellView {
	// models and images are open to every account; metrics picks its section
	// by tab; admin and every other view share their section's name.
	if (view === 'models' || view === 'images') return view;
	// The account view needs a signed-in account. AUTH_MODE=none has a null
	// set and no account, so its one settings view is the turn-on card, which
	// localMode keeps open; every other null view is still refused.
	if (view === 'account')
		return capabilities === null && !localMode ? refusedView(capabilities) : view;
	const section: GatedSection = view === 'metrics' ? metricsSection(tab) : view;
	if (sectionNeeds(section, capabilities) === null) return view;
	return refusedView(capabilities);
}
