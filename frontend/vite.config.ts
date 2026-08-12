import adapter from '@sveltejs/adapter-static';
import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { waitlistProxy } from './vite.waitlist-proxy.js';

// The dev loop runs the API natively (docs/local-development.md); the SPA
// calls it with relative /api/v1 paths in every deployment. Linked worktrees
// get their own ports from the Makefile (scripts/checkout-ports.sh), so read
// them from the environment and fall back to the documented single-checkout
// values when unset.
const apiPort = process.env.API_PORT ?? '8000';
const webPort = process.env.WEB_PORT ?? '5173';

const apiProxy = {
	'/api/v1': { target: `http://localhost:${apiPort}`, changeOrigin: true, ws: true }
};

export default defineConfig({
	// strictPort: the API allowlists this exact origin for the WebSocket
	// endpoints (ALLOWED_ORIGINS in the Makefile), so a silent fallback to
	// another port when the configured one is taken would produce an opaque
	// 403 instead. The port is per-checkout now, but it must still fail loudly.
	server: { port: Number(webPort), strictPort: true, proxy: apiProxy },
	preview: { proxy: apiProxy },
	plugins: [
		waitlistProxy(),
		tailwindcss(),
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// Bound stale-shell detection without polling an idle tab aggressively.
			version: {
				pollInterval: 5 * 60 * 1000
			},

			// Static build: every known route is prerendered. The fallback document
			// is the error page, so a CDN answers an unknown path with a real 404
			// carrying this project's page rather than a 200 app shell that a
			// crawler reads as a soft 404. The API server still falls back to
			// index.html for unknown GET paths in self-hosted mode.
			adapter: adapter({ fallback: '404.html' }),

			// Hash-mode CSP for prerendered pages: SvelteKit injects a SHA-256
			// script-src hash for its inline bootstrap. HTTP responses still
			// emit a looser script-src (with 'unsafe-inline') as a compatibility
			// envelope; the meta policy is the effective script restriction.
			// style-src keeps 'unsafe-inline' for inline style attributes and
			// runtime chart styles. Source lists match backend/app/security.py
			// except script-src, which stays 'self' here so hashes can be added.
			csp: {
				mode: 'hash',
				directives: {
					'default-src': ['self'],
					'base-uri': ['self'],
					'object-src': ['none'],
					'frame-ancestors': ['none'],
					'form-action': ['self'],
					'frame-src': ['self'],
					'connect-src': ['self'],
					'font-src': ['self'],
					'img-src': ['self', 'https:', 'http:'],
					'script-src': ['self'],
					'style-src': ['self', 'unsafe-inline']
				}
			}
		})
	]
});
