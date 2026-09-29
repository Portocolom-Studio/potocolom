// node --test runs plain .ts through V8 type stripping, but a .svelte.ts module
// only means something after the Svelte compiler rewrote its runes, and its
// $lib/$app imports are SvelteKit aliases Node cannot resolve. npm test loads
// this file with --import: the first copy registers the hooks, the second copy
// (loaded in the hooks thread, hence the ?hooks suffix) is the hook module.

import { readFileSync, statSync } from 'node:fs';
import { register, stripTypeScriptTypes } from 'node:module';
import { fileURLToPath } from 'node:url';

const HOOKS_COPY = import.meta.url.endsWith('?hooks');
if (HOOKS_COPY) {
	// This thread only loads this file and the Svelte compiler, so the only
	// warning it can raise is the ExperimentalWarning of stripTypeScriptTypes,
	// which would print once in every test process for nothing.
	process.removeAllListeners('warning');
	process.on('warning', (warning) => {
		if (!String(warning).includes('stripTypeScriptTypes')) process.stderr.write(`${warning}\n`);
	});
} else {
	await register('./node-test-loader.mjs?hooks', import.meta.url);
}

const LIB_ROOT = new URL('../src/lib/', import.meta.url);
const STUB_ROOT = new URL('./test-stubs/', import.meta.url);
const STUBS = new Map([
	['$app/navigation', 'navigation.ts'],
	['$app/paths', 'paths.ts'],
	['$app/state', 'state.ts'],
	['$env/static/public', 'public-env.ts']
]);
const EXTENSIONS = ['.ts', '.svelte.ts', '.js'];

function isFile(url) {
	try {
		return statSync(fileURLToPath(url)).isFile();
	} catch {
		return false;
	}
}

// The source imports its siblings without an extension the way SvelteKit's
// resolver allows, which the default resolver does not.
function extensionlessFile(base) {
	for (const extension of EXTENSIONS) {
		const url = new URL(base.href + extension);
		if (isFile(url)) return url;
	}
	return null;
}

export async function resolve(specifier, context, nextResolve) {
	const stub = STUBS.get(specifier);
	if (stub !== undefined) return { url: new URL(stub, STUB_ROOT).href, shortCircuit: true };
	if (specifier.startsWith('$lib/')) {
		const base = new URL(specifier.slice('$lib/'.length), LIB_ROOT);
		const url = isFile(base) ? base : extensionlessFile(base);
		if (url !== null) return { url: url.href, shortCircuit: true };
	}
	if (
		(specifier.startsWith('./') || specifier.startsWith('../')) &&
		context.parentURL !== undefined
	) {
		const exact = new URL(specifier, context.parentURL);
		if (!isFile(exact)) {
			const url = extensionlessFile(exact);
			if (url !== null) return { url: url.href, shortCircuit: true };
		}
	}
	return nextResolve(specifier, context);
}

export async function load(url, context, nextLoad) {
	if (url.startsWith('file:')) {
		const pathname = new URL(url).pathname;
		if (pathname.endsWith('.svelte.ts')) {
			const filename = fileURLToPath(url);
			const js = stripTypeScriptTypes(readFileSync(filename, 'utf8'), { mode: 'strip' });
			const { compileModule } = await import('svelte/compiler');
			const compiled = compileModule(js, { filename, generate: 'client' });
			return { format: 'module', source: compiled.js.code, shortCircuit: true };
		}
		// i18n.svelte.ts imports its dictionaries without an import attribute,
		// which the default loader would reject, so hand them over as a module.
		if (pathname.endsWith('.json')) {
			return {
				format: 'module',
				source: `export default ${readFileSync(fileURLToPath(url), 'utf8')}`,
				shortCircuit: true
			};
		}
	}
	return nextLoad(url, context);
}
