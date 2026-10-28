import assert from 'node:assert/strict';
import { test } from 'node:test';
import { accountCacheKey, revalidationDue, SwrCache } from './swr-cache.ts';

test('SwrCache returns what was written, however old', () => {
	const cache = new SwrCache();
	assert.equal(cache.read('models'), undefined);
	cache.write('models', ['a', 'b'], 1_000);
	assert.deepEqual(cache.read<string[]>('models'), ['a', 'b']);
	cache.write('models', ['c'], 2_000);
	assert.deepEqual(cache.read<string[]>('models'), ['c']);
});

test('SwrCache reports the age of an entry', () => {
	const cache = new SwrCache();
	assert.equal(cache.age('metrics', 5_000), null);
	cache.write('metrics', 1, 1_000);
	assert.equal(cache.age('metrics', 4_000), 3_000);
	// A clock that steps back never reports a negative age.
	assert.equal(cache.age('metrics', 500), 0);
});

test('SwrCache keeps keys apart and forgets on delete and clear', () => {
	const cache = new SwrCache();
	cache.write('usage:30d', 'month');
	cache.write('usage:24h', 'day');
	assert.equal(cache.read('usage:30d'), 'month');
	assert.equal(cache.read('usage:24h'), 'day');
	cache.delete('usage:30d');
	assert.equal(cache.read('usage:30d'), undefined);
	assert.equal(cache.read('usage:24h'), 'day');
	cache.clear();
	assert.equal(cache.read('usage:24h'), undefined);
});

test('SwrCache stores falsy values as values', () => {
	const cache = new SwrCache();
	cache.write('count', 0);
	cache.write('list', []);
	assert.equal(cache.read('count'), 0);
	assert.deepEqual(cache.read('list'), []);
	assert.notEqual(cache.age('count'), null);
});

test('accountCacheKey keeps accounts apart', () => {
	assert.notEqual(
		accountCacheKey('550e8400-e29b-41d4-a716-446655440000', 'metrics:failures'),
		accountCacheKey('550e8400-e29b-41d4-a716-446655440001', 'metrics:failures')
	);
	assert.notEqual(accountCacheKey(null, 'metrics:failures'), 'metrics:failures');
});

test('SwrCache.clear drops owner and shared entries at an owner boundary', () => {
	const cache = new SwrCache();
	const mine = accountCacheKey('550e8400-e29b-41d4-a716-446655440000', 'metrics:usage-me:30d');
	const noAccount = accountCacheKey(null, 'metrics:failures');
	cache.write(mine, { events: 3 });
	cache.write(noAccount, []);
	cache.write('metrics:benchmark-sessions', ['s1']);
	cache.clear();
	assert.equal(cache.read(mine), undefined);
	assert.equal(cache.read(noAccount), undefined);
	assert.equal(cache.read('metrics:benchmark-sessions'), undefined);
});

test('revalidationDue throttles focus refreshes', () => {
	assert.equal(revalidationDue(null, 1_000, 30_000), true);
	assert.equal(revalidationDue(1_000, 10_000, 30_000), false);
	assert.equal(revalidationDue(1_000, 31_000, 30_000), true);
	assert.equal(revalidationDue(1_000, 40_000, 30_000), true);
});
