// See https://svelte.dev/docs/kit/types#app.d.ts
// for information about these interfaces
declare global {
	namespace App {
		// interface Error {}
		// interface Locals {}
		// interface PageData {}
		interface PageState {
			/** The transient studio UI open on this history entry; Back closes it
			 * (see $lib/overlay-history.ts). */
			studioOverlay?: 'inspector' | 'command' | 'sidebar';
		}
		// interface Platform {}
	}
}

export {};
