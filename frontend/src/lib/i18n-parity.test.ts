import assert from 'node:assert/strict';
import { test } from 'node:test';

import en from './i18n/en.json';
import es from './i18n/es.json';

const enMessages: Record<string, string> = en;
const esMessages: Record<string, string> = es;

// A placeholder is any {name} token, dotted names included ({entry.title}).
// The sorted set of names is what must match between the two languages.
function placeholders(message: string): string {
	const names = [...message.matchAll(/\{([^{}]+)\}/g)].map((match) => match[1]);
	return [...new Set(names)].sort().join(',');
}

test('en.json and es.json have the same keys', () => {
	const missingInEs = Object.keys(enMessages).filter((key) => !(key in esMessages));
	const missingInEn = Object.keys(esMessages).filter((key) => !(key in enMessages));
	assert.deepEqual(
		missingInEs,
		[],
		`keys in en.json but missing from es.json: ${missingInEs.join(', ')}`
	);
	assert.deepEqual(
		missingInEn,
		[],
		`keys in es.json but missing from en.json: ${missingInEn.join(', ')}`
	);
});

test('each key has the same placeholders in en.json and es.json', () => {
	const differ = Object.keys(enMessages).filter(
		(key) => key in esMessages && placeholders(enMessages[key]) !== placeholders(esMessages[key])
	);
	assert.deepEqual(
		differ,
		[],
		`placeholders differ between en.json and es.json for: ${differ.join(', ')}`
	);
});
