#!/usr/bin/env node
/**
 * Post-build check: every prerendered HTML document must carry a hash CSP meta
 * policy whose script-src is exactly 'self' plus SHA-256 hashes of that
 * document's inline scripts (no unsafe-inline, no extra sources).
 * Dependency-free; invoked by `npm run build`.
 */
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const buildDir = join(root, 'build');

function walkHtml(dir) {
	const out = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) out.push(...walkHtml(path));
		else if (entry.name.endsWith('.html')) out.push(path);
	}
	return out;
}

function scriptHash(body) {
	return `'sha256-${createHash('sha256').update(body, 'utf8').digest('base64')}'`;
}

function fail(message) {
	console.error(`verify-csp: ${message}`);
	process.exit(1);
}

let htmlFiles;
try {
	htmlFiles = walkHtml(buildDir);
} catch (err) {
	fail(`cannot read ${buildDir}: ${err.message}`);
}

if (htmlFiles.length === 0) {
	fail('no HTML files under build/');
}

for (const filePath of htmlFiles) {
	const rel = relative(buildDir, filePath);
	const html = readFileSync(filePath, 'utf8');

	// Attribute values use double quotes; the policy itself contains single quotes.
	const metaMatch = html.match(
		/<meta\s+http-equiv=["']content-security-policy["']\s+content=(["'])([\s\S]*?)\1/i
	);
	if (!metaMatch) {
		fail(`${rel}: missing Content-Security-Policy meta tag`);
	}

	const policy = metaMatch[2];
	const scriptSrc = policy
		.split(';')
		.map((part) => part.trim())
		.find((part) => part.toLowerCase().startsWith('script-src '));

	if (!scriptSrc) {
		fail(`${rel}: CSP meta policy has no script-src directive`);
	}

	if (/'unsafe-inline'/.test(scriptSrc)) {
		fail(`${rel}: script-src must not allow 'unsafe-inline':\n  ${scriptSrc}`);
	}

	const tokens = scriptSrc.slice('script-src '.length).trim().split(/\s+/).filter(Boolean);
	const expected = new Set(["'self'"]);

	// Inline scripts only (no src=). Match body exactly for CSP hashing.
	const inlineRe = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
	let match;
	while ((match = inlineRe.exec(html)) !== null) {
		expected.add(scriptHash(match[1]));
	}

	if (expected.size === 1) {
		fail(`${rel}: expected at least one inline bootstrap script to hash`);
	}

	const actual = new Set(tokens);
	const missing = [...expected].filter((t) => !actual.has(t));
	const extra = [...actual].filter((t) => !expected.has(t));
	if (missing.length || extra.length) {
		fail(
			`${rel}: script-src must be exactly 'self' plus hashes of inline scripts.\n` +
				`  policy:  ${scriptSrc}\n` +
				`  missing: ${missing.join(' ') || '(none)'}\n` +
				`  extra:   ${extra.join(' ') || '(none)'}`
		);
	}
}

console.log(
	`verify-csp: ok (${htmlFiles.length} HTML file(s); script-src is 'self' + matching SHA-256 hashes)`
);
