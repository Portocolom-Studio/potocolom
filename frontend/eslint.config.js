import js from '@eslint/js';
import prettier from 'eslint-config-prettier';
import svelte from 'eslint-plugin-svelte';
import globals from 'globals';
import ts from 'typescript-eslint';

export default ts.config(
	{ ignores: ['.svelte-kit/', 'build/', 'static/'] },
	{ linterOptions: { reportUnusedDisableDirectives: 'error' } },
	js.configs.recommended,
	...ts.configs.recommended,
	...svelte.configs.recommended,
	prettier,
	...svelte.configs.prettier,
	{
		languageOptions: { globals: { ...globals.browser, ...globals.node } },
		rules: {
			// TypeScript already reports undefined names, with type information.
			'no-undef': 'off',
			// An empty catch is how this code ignores a storage or observer API that throws.
			'no-empty': ['error', { allowEmptyCatch: true }],
			'@typescript-eslint/ban-ts-comment': ['error', { 'ts-nocheck': 'allow-with-description' }],
			'@typescript-eslint/no-unused-vars': [
				'error',
				{ argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' }
			],
			// The app is served from the site root (no paths.base), so resolve() would change nothing.
			'svelte/no-navigation-without-resolve': 'off',
			// It flags function-local and module-level bookkeeping Sets and Maps that never reach
			// reactive state, and copies assigned to $state that are already reactive.
			'svelte/prefer-svelte-reactivity': 'off',
			// {' '} keeps a space between inline elements that formatting would move.
			'svelte/no-useless-mustaches': 'off'
		}
	},
	{
		files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
		languageOptions: {
			parserOptions: {
				extraFileExtensions: ['.svelte'],
				parser: ts.parser
			}
		},
		rules: {
			// A bare read at the top of an $effect is how Svelte 5 tracks a dependency.
			'@typescript-eslint/no-unused-expressions': 'off'
		}
	},
	{
		// Build-time HTML from this repository: rendered docs Markdown, inline icons and JSON-LD.
		files: ['src/routes/(public-site)/docs/**/*.svelte', 'src/lib/components/Seo.svelte'],
		rules: { 'svelte/no-at-html-tags': 'off' }
	}
);
