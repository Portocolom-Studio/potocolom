const SHELL_VIEWS = [
	'generate',
	'image_to_image',
	'upscale',
	'edit_image',
	'image_to_text',
	'realtime_canvas',
	'images',
	'models',
	'metrics'
] as const;
const METRICS_TABS = ['usage', 'benchmarks'] as const;

export type ShellView = (typeof SHELL_VIEWS)[number];
export type MetricsTab = (typeof METRICS_TABS)[number];

export function readStudioView(url: URL): { view: ShellView; tab: MetricsTab } {
	const view = url.searchParams.get('view');
	const tab = url.searchParams.get('tab');
	return {
		view: SHELL_VIEWS.find((known) => known === view) ?? 'generate',
		tab: METRICS_TABS.find((known) => known === tab) ?? 'usage'
	};
}

export function studioViewSearch(search: string, view: ShellView, tab: MetricsTab): string {
	const params = new URLSearchParams(search);
	// set() keeps a parameter where it was, so the current view builds the
	// current search and openView can skip it as a no-op.
	if (view === 'generate') params.delete('view');
	else params.set('view', view);
	if (view === 'metrics' && tab !== 'usage') params.set('tab', tab);
	else params.delete('tab');
	const query = params.toString();
	return query === '' ? '' : `?${query}`;
}
