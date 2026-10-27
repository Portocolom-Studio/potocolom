// The Metrics and Administration views are separate chunks (the chart stack
// is the largest JS in the studio), loaded when the view opens. Pointing at
// or focusing their sidebar entry starts the download early, so the view
// usually opens without waiting. One promise per chunk: the shell renders
// from the same one, and a failed download is retried on the next ask.

const loaders = {
	metrics: () => import('$lib/components/studio-metrics-dashboard.svelte'),
	admin: () => import('$lib/components/studio-admin-dashboard.svelte')
};

export type LazyView = keyof typeof loaders;

const pending = new Map<LazyView, ReturnType<(typeof loaders)[LazyView]>>();

export function loadLazyView<View extends LazyView>(
	view: View
): ReturnType<(typeof loaders)[View]> {
	let promise = pending.get(view);
	if (promise === undefined) {
		promise = loaders[view]();
		pending.set(view, promise);
		promise.catch(() => {
			if (pending.get(view) === promise) pending.delete(view);
		});
	}
	return promise as ReturnType<(typeof loaders)[View]>;
}

export function prefetchView(view: LazyView): void {
	void loadLazyView(view).catch(() => {
		// Opening the view asks again and shows its own error.
	});
}
