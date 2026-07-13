#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const host = process.env.COLLAGE_HOST ?? '127.0.0.1';

const variants = [
	{ id: 'masonry', port: 5190 },
	{ id: 'masonry-2col', port: 5191 },
	{ id: 'masonry-3col', port: 5192 },
	{ id: 'masonry-4col', port: 5193 },
	{ id: 'masonry-tight', port: 5194 },
	{ id: 'masonry-loose', port: 5195 },
	{ id: 'masonry-editorial', port: 5196 },
	{ id: 'graph', port: 5197 }
];

const children = [];

function startServer({ port, strictPort = true }) {
	const child = spawn(
		'npm',
		['run', 'dev', '--', '--host', host, '--port', String(port), '--strictPort'],
		{
			cwd: root,
			stdio: 'inherit',
			env: { ...process.env, FORCE_COLOR: '1' }
		}
	);
	children.push(child);
	return child;
}

function shutdown(code = 0) {
	for (const child of children) {
		child.kill('SIGTERM');
	}
	setTimeout(() => process.exit(code), 250);
}

process.on('SIGINT', () => shutdown(0));
process.on('SIGTERM', () => shutdown(0));

const hubPort = 5189;
startServer({ port: hubPort, strictPort: true });

for (const variant of variants) {
	startServer({ port: variant.port, strictPort: true });
}

console.log('');
console.log('Collage compare servers:');
console.log(`  Hub (all variants): http://${host}:${hubPort}/collage-preview`);
for (const variant of variants) {
	console.log(
		`  ${variant.id.padEnd(18)} http://${host}:${variant.port}/collage-preview/${variant.id}`
	);
}
console.log('');
console.log('Press Ctrl+C to stop all servers.');
