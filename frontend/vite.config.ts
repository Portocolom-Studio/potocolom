import adapter from '@sveltejs/adapter-static';
import tailwindcss from '@tailwindcss/vite';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { waitlistProxy } from './vite.waitlist-proxy.js';

// The dev loop runs the API natively on :8000 (docs/local-development.md);
// the SPA calls it with relative /api/v1 paths in every deployment.
const apiProxy = {
	'/api/v1': { target: 'http://localhost:8000', changeOrigin: true, ws: true }
};

export default defineConfig({
	server: { proxy: apiProxy },
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

			// Static SPA build: one artifact for every deployment, served by the API
			// when self-hosted and by a CDN in the cloud (docs/architecture.md).
			adapter: adapter({ fallback: 'index.html' }),

			// Hash-mode CSP for prerendered pages: SvelteKit injects a SHA-256
			// script-src hash for its inline bootstrap. HTTP responses still
			// emit a looser script-src (with 'unsafe-inline') as a compatibility
			// envelope; the meta policy is the effective script restriction.
			// style-src keeps 'unsafe-inline' for inline style attributes and
			// runtime chart styles. Aligned with backend/app/security.py except
			// script-src (docs/internals/08-security-production.md).
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
					'img-src': ['self', 'https:'],
					'script-src': ['self'],
					'style-src': ['self', 'unsafe-inline']
				}
			}
		})
	]
});
