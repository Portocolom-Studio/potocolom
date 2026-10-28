// The studio top bar belongs to the shell, but its middle and right-hand
// controls belong to whichever view is open (tabs for Metrics, the service
// switch for Generate, Connect for the canvas). A view hands the shell its
// snippets here for as long as it is mounted.
import type { Component, Snippet } from 'svelte';

/** An action of the open view, listed in the command palette. */
export type ViewCommand = {
	id: string;
	label: string;
	hint?: string;
	icon: Component;
	run: () => void;
};

export const studioChrome = $state({
	center: null as Snippet | null,
	actions: null as Snippet | null,
	commands: [] as ViewCommand[]
});

/** Call during a view's component init. The snippets are read through the
 * getter so a view can swap them reactively; both clear when it unmounts. */
export function useStudioHeader(get: () => { center?: Snippet; actions?: Snippet }): void {
	$effect(() => {
		const { center = null, actions = null } = get();
		studioChrome.center = center;
		studioChrome.actions = actions;
		return () => {
			if (studioChrome.center === center) studioChrome.center = null;
			if (studioChrome.actions === actions) studioChrome.actions = null;
		};
	});
}

/** Call during a view's component init: the palette lists these commands
 * while the view is mounted, read through the getter so they can change. */
export function useViewCommands(get: () => ViewCommand[]): void {
	$effect(() => {
		const commands = get();
		studioChrome.commands = commands;
		return () => {
			if (studioChrome.commands === commands) studioChrome.commands = [];
		};
	});
}
