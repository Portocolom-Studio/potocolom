import assert from 'node:assert/strict';
import { test } from 'node:test';

// The shadcn components import '$lib/utils.js' while the file is utils.ts, as
// SvelteKit allows; the test loader has to resolve that the same way.
test('the test loader resolves a .js specifier to its .ts file', async () => {
	const { cn } = await import('$lib/utils.js');
	assert.equal(cn('px-2', 'px-4'), 'px-4');
});
