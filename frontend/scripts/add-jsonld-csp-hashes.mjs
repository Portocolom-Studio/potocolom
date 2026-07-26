#!/usr/bin/env node
/**
 * Post-build step: add the SHA-256 hash of every inline JSON-LD block to that
 * document's CSP script-src.
 *
 * SvelteKit's hash-mode CSP only hashes the scripts it injects itself, so
 * structured data written in svelte:head is left out of the policy and
 * verify-csp.mjs rejects the build. JSON-LD is data that browsers never
 * execute, so hashing it grants no execution rights.
 *
 * Only type="application/ld+json" blocks are hashed, and hashes are added to
 * the existing policy rather than replacing it. Any other unexpected inline
 * script therefore still fails verify-csp.mjs, which is the check this step
 * must not weaken.
 */
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const buildDir = join(root, 'build');

const JSON_LD_RE =
	/<script(?![^>]*\bsrc=)[^>]*\btype=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
const META_RE = /(<meta\s+http-equiv=["']content-security-policy["']\s+content=")([^"]*)(")/i;

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

function withHashes(policy, hashes, file) {
	const directives = policy.split(';');
	const index = directives.findIndex((d) => d.trim().toLowerCase().startsWith('script-src'));
	if (index === -1) {
		throw new Error(`${file}: CSP meta policy has no script-src directive`);
	}
	const tokens = directives[index].trim().split(/\s+/);
	for (const value of hashes) {
		if (!tokens.includes(value)) tokens.push(value);
	}
	directives[index] = ` ${tokens.join(' ')}`;
	return directives.join(';');
}

for (const file of walkHtml(buildDir)) {
	const html = readFileSync(file, 'utf8');
	const hashes = [...html.matchAll(JSON_LD_RE)].map((match) => hash(match[1]));
	if (hashes.length === 0) continue;

	const meta = html.match(META_RE);
	if (!meta) {
		throw new Error(`${relative(buildDir, file)}: missing Content-Security-Policy meta tag`);
	}

	const policy = withHashes(meta[2], hashes, relative(buildDir, file));
	writeFileSync(file, html.replace(META_RE, `$1${policy}$3`));
}
