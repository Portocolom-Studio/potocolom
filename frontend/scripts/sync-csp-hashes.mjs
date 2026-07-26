#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const buildDir = join(root, 'build');

function walkHtml(dir) {
	const files = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) files.push(...walkHtml(path));
		else if (entry.name.endsWith('.html')) files.push(path);
	}
	return files;
}

function hash(body) {
	return `'sha256-${createHash('sha256').update(body, 'utf8').digest('base64')}'`;
}

for (const file of walkHtml(buildDir)) {
	const html = readFileSync(file, 'utf8');
	const inlineRe = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
	const hashes = new Set();
	let match;
	while ((match = inlineRe.exec(html)) !== null) hashes.add(hash(match[1]));

	const scriptSrc = ["'self'", ...hashes].join(' ');
	let foundPolicy = false;
	const updated = html.replace(
		/(<meta\s+http-equiv=["']content-security-policy["']\s+content=["'][\s\S]*?script-src)\s+[^;]+/i,
		(_policy, prefix) => {
			foundPolicy = true;
			return `${prefix} ${scriptSrc}`;
		}
	);

	if (!foundPolicy) {
		throw new Error(`Missing hash CSP meta tag in ${file}`);
	}
	writeFileSync(file, updated);
}
