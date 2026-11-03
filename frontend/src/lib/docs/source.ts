// The only module that reads the Markdown files. Vite inlines them at build time.
import { buildDocsIndex } from './model';

const files = import.meta.glob('./pages/**/*.md', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

export const docsIndex = buildDocsIndex(files);
