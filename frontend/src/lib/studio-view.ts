const SHELL_VIEWS = [
	'generate',
	'image_to_image',
	'upscale',
	'edit_image',
	'image_to_text',
	'realtime_canvas',
	'images',
	'models',
	'metrics',
	'admin'
] as const;
const METRICS_TABS = ['usage', 'benchmarks', 'mine'] as const;
export const ADMIN_TABS = ['users', 'audit', 'fleet', 'banner'] as const;

export type ShellView = (typeof SHELL_VIEWS)[number];
export type MetricsTab = (typeof METRICS_TABS)[number];
export type AdminTab = (typeof ADMIN_TABS)[number];
export type StudioTab = MetricsTab | AdminTab;

export function readStudioView(url: URL): { view: ShellView; tab: StudioTab } {
	const view = url.searchParams.get('view');
	const tab = url.searchParams.get('tab');
	const knownView = SHELL_VIEWS.find((known) => known === view) ?? 'generate';
	return {
		view: knownView,
		tab:
			knownView === 'admin'
				? (ADMIN_TABS.find((known) => known === tab) ?? 'users')
				: (METRICS_TABS.find((known) => known === tab) ?? 'usage')
	};
}

export function studioViewSearch(search: string, view: ShellView, tab: StudioTab): string {
	const params = new URLSearchParams(search);
	// set() keeps a parameter where it was, so the current view builds the
	// current search and openView can skip it as a no-op.
	if (view === 'generate') params.delete('view');
	else params.set('view', view);
	if (view === 'metrics' && (tab === 'benchmarks' || tab === 'mine')) params.set('tab', tab);
	else if (view === 'admin' && (tab === 'audit' || tab === 'fleet' || tab === 'banner'))
		params.set('tab', tab);
	else params.delete('tab');
	const query = params.toString();
	return query === '' ? '' : `?${query}`;
}
