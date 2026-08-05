#!/usr/bin/env node
/**
 * Post-build check: every prerendered HTML document must carry a hash CSP meta
 * policy whose non-script directives match the configured baseline and whose
 * script-src is exactly 'self' plus SHA-256 hashes of that document's inline
 * scripts (no unsafe-inline, no extra sources).
 * Dependency-free; invoked by `npm run build`.
 */
import { createHash } from 'node:crypto';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const buildDir = join(root, 'build');

// Matches frontend/vite.config.ts csp.directives except script-src (hashed per
// file) and frame-ancestors (SvelteKit omits it from meta CSP; browsers ignore
// frame-ancestors in meta anyway).
const EXPECTED_DIRECTIVES = {
	'default-src': ["'self'"],
	'base-uri': ["'self'"],
	'object-src': ["'none'"],
	'form-action': ["'self'"],
	'frame-src': ["'self'"],
	'connect-src': ["'self'"],
	'font-src': ["'self'"],
	'img-src': ["'self'", 'https:', 'http:'],
	'style-src': ["'self'", "'unsafe-inline'"]
};

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

function parsePolicy(policy) {
	/** @type {Record<string, string[]>} */
	const directives = {};
	for (const part of policy.split(';')) {
		const trimmed = part.trim();
		if (!trimmed) continue;
		const [name, ...values] = trimmed.split(/\s+/);
		directives[name.toLowerCase()] = values;
	}
	return directives;
}

function sameTokens(actual, expected) {
	if (actual.length !== expected.length) return false;
	const a = [...actual].sort();
	const e = [...expected].sort();
	return a.every((token, i) => token === e[i]);
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

	const directives = parsePolicy(metaMatch[2]);

	for (const [name, expected] of Object.entries(EXPECTED_DIRECTIVES)) {
		const actual = directives[name];
		if (!actual) {
			fail(`${rel}: CSP meta policy missing ${name}`);
		}
		if (!sameTokens(actual, expected)) {
			fail(
				`${rel}: ${name} mismatch.\n` +
					`  expected: ${expected.join(' ')}\n` +
					`  actual:   ${actual.join(' ')}`
			);
		}
	}

	const scriptSrc = directives['script-src'];
	if (!scriptSrc) {
		fail(`${rel}: CSP meta policy has no script-src directive`);
	}
	if (scriptSrc.includes("'unsafe-inline'")) {
		fail(`${rel}: script-src must not allow 'unsafe-inline':\n  script-src ${scriptSrc.join(' ')}`);
	}

	const expectedScript = new Set(["'self'"]);
	const inlineRe = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
	let match;
	while ((match = inlineRe.exec(html)) !== null) {
		expectedScript.add(scriptHash(match[1]));
	}
	if (expectedScript.size === 1) {
		fail(`${rel}: expected at least one inline bootstrap script to hash`);
	}

	const actualScript = new Set(scriptSrc);
	const missing = [...expectedScript].filter((t) => !actualScript.has(t));
	const extra = [...actualScript].filter((t) => !expectedScript.has(t));
	if (missing.length || extra.length) {
		fail(
			`${rel}: script-src must be exactly 'self' plus hashes of inline scripts.\n` +
				`  policy:  script-src ${scriptSrc.join(' ')}\n` +
				`  missing: ${missing.join(' ') || '(none)'}\n` +
				`  extra:   ${extra.join(' ') || '(none)'}`
		);
	}

	// frame-ancestors is ignored in meta CSP; allow absence or presence, but
	// reject any other unexpected directive that would widen the policy.
	for (const name of Object.keys(directives)) {
		if (name === 'script-src' || name === 'frame-ancestors') continue;
		if (!(name in EXPECTED_DIRECTIVES)) {
			fail(`${rel}: unexpected CSP directive ${name}`);
		}
	}
}

console.log(
	`verify-csp: ok (${htmlFiles.length} HTML file(s); meta CSP directives and script hashes match)`
);
