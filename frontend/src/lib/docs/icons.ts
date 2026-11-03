// Icons for the docs site: one stroke set, 24x24 grid, drawn with currentColor.
// The markup comes from this file only, so injecting it with {@html} is safe.

const ICON_BODY = {
	activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
	book: '<path d="M4 5h6a2 2 0 0 1 2 2v12a2 2 0 0 0-2-2H4z"/><path d="M20 5h-6a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h6z"/>',
	code: '<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/>',
	cpu: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
	database:
		'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
	download: '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
	eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
	file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
	'hard-drive':
		'<rect x="3" y="12" width="18" height="7" rx="2"/><path d="M3 12l3-7h12l3 7M7 16h.01"/>',
	layers: '<path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/>',
	list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
	lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
	refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/>',
	rocket:
		'<path d="M5 15c-1.5 1.2-2 3-2 5 2 0 3.8-.5 5-2"/><path d="M9 14l-2-2c.6-3.6 3.2-7.2 9-10 0 5.8-3.4 8.4-7 9z"/><circle cx="15" cy="9" r="1.5"/>',
	search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
	server:
		'<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 7h.01M8 18h.01"/>',
	settings:
		'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>',
	share:
		'<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.2 10.8l7.6-3.6M8.2 13.2l7.6 3.6"/>',
	shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/>',
	terminal: '<path d="M4 17l6-6-6-6M12 19h8"/>',
	user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
	globe:
		'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'
} as const;

export type IconName = keyof typeof ICON_BODY;

export function iconSvg(name: string): string {
	if (!(name in ICON_BODY)) throw new Error(`docs: unknown icon "${name}"`);
	const body = ICON_BODY[name as IconName];
	return `<svg class="docs-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

// One icon per page, keyed by "tab/slug". A page with no entry fails the build.
const PAGE_ICONS: Record<string, IconName> = {
	'guide/introduction': 'book',
	'guide/quickstart': 'rocket',
	'guide/hardware-requirements': 'cpu',
	'self-hosting/installing-a-release': 'download',
	'self-hosting/checking-a-machine': 'shield',
	'self-hosting/gpu-passthrough': 'cpu',
	'self-hosting/reaching-the-studio': 'globe',
	'self-hosting/remote-gpu-workers': 'server',
	'self-hosting/accounts': 'user',
	'self-hosting/tls-and-hsts': 'lock',
	'self-hosting/gated-models': 'eye',
	'self-hosting/backup-and-restore': 'database',
	'self-hosting/what-persists-where': 'hard-drive',
	'self-hosting/logs': 'list',
	'self-hosting/updating': 'refresh',
	'api/overview': 'code',
	'api/authentication': 'lock',
	'api/endpoints': 'layers',
	'api/realtime-socket': 'activity',
	'api/account-states': 'user',
	'api/sharing': 'share',
	'architecture/overview': 'layers',
	'architecture/realtime-loop': 'activity',
	'architecture/gpu-scheduling': 'cpu',
	'architecture/deployment-profiles': 'server',
	'contributing/local-development': 'terminal',
	'contributing/testing-ladder': 'eye',
	'contributing/self-hosted-runner': 'settings',
	'contributing/security-checklist': 'shield',
	'reference/decisions': 'file'
};

export function pageIcon(tab: string, slug: string): string {
	const name = PAGE_ICONS[`${tab}/${slug}`];
	if (!name) throw new Error(`docs: no icon for ${tab}/${slug}`);
	return iconSvg(name);
}
