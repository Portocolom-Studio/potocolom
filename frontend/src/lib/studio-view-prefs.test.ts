import assert from 'node:assert/strict';
import test from 'node:test';
import {
	activateViewPrefsOwner,
	defaultViewPrefs,
	hasViewPrefs,
	parseViewPrefs,
	readViewPrefs,
	viewPrefsKey,
	writeViewPrefs,
	type PrefStorage,
	type ViewOwner
} from './studio-view-prefs.ts';

class MemoryStorage implements PrefStorage {
	#values = new Map<string, string>();
	get length() {
		return this.#values.size;
	}
	key(index: number) {
		return [...this.#values.keys()][index] ?? null;
	}
	getItem(key: string) {
		return this.#values.get(key) ?? null;
	}
	setItem(key: string, value: string) {
		this.#values.set(key, value);
	}
	removeItem(key: string) {
		this.#values.delete(key);
	}
}

const a: ViewOwner = { kind: 'account', id: '550e8400-e29b-41d4-a716-446655440000' };
const b: ViewOwner = { kind: 'account', id: '550e8400-e29b-41d4-a716-446655440001' };

test('section keys scope preferences to an owner and section', () => {
	assert.notEqual(viewPrefsKey(a, 'metrics'), viewPrefsKey(b, 'metrics'));
	assert.notEqual(viewPrefsKey(a, 'metrics'), viewPrefsKey(a, 'images'));
	assert.deepEqual(readViewPrefs(null, 'metrics'), defaultViewPrefs('metrics'));
});

test('each section normalizes values and keeps malformed input bounded', () => {
	const metrics = parseViewPrefs('metrics', {
		version: 1,
		section: 'metrics',
		value: {
			tab: 'bad',
			usageRange: '30d',
			mineRange: 'bad',
			selectedSessionId: '2026-10-27T12:34:56.123Z'
		}
	});
	assert.deepEqual(metrics, {
		tab: 'usage',
		usageRange: '30d',
		mineRange: '30d',
		selectedSessionId: '2026-10-27T12:34:56.123Z'
	});
	const images = parseViewPrefs('images', {
		version: 1,
		section: 'images',
		value: {
			mode: 'lineage',
			action: 'upscale',
			search: 'q'.repeat(1100),
			filters: { range: 'custom', from: '2026-02-30', through: '2026-03-01' }
		}
	}) as { mode: string; search: string; filters: { range: string; from: string } };
	assert.equal(images.mode, 'lineage');
	assert.equal(images.search.length, 1000);
	assert.equal(images.filters.range, 'all');
	assert.equal(images.filters.from, '');
	for (const [from, through] of [
		['2026-01-01', ''],
		['', '2026-01-02'],
		['2026-02-02', '2026-02-01']
	]) {
		const invalid = parseViewPrefs('images', {
			version: 1,
			section: 'images',
			value: { filters: { range: 'custom', from, through, models: [''] } }
		});
		assert.equal(invalid.filters.range, 'all');
		assert.deepEqual(invalid.filters.models, []);
	}
	const valid = parseViewPrefs('images', {
		version: 1,
		section: 'images',
		value: { filters: { range: 'custom', from: '2026-02-01', through: '2026-02-02' } }
	});
	assert.deepEqual(
		[valid.filters.range, valid.filters.from, valid.filters.through],
		['custom', '2026-02-01', '2026-02-02']
	);
	const tooManyModels = parseViewPrefs('images', {
		version: 1,
		section: 'images',
		value: { filters: { models: Array.from({ length: 140 }, (_, index) => `model-${index}`) } }
	});
	assert.equal(tooManyModels.filters.models.length, 128);
	const invalidIds = parseViewPrefs('images', {
		version: 1,
		section: 'images',
		value: {
			gridAnchor: { id: 'job-1', beforeCursor: 'cursor-1', offset: 0 },
			gridSelectionId: 'asset-1',
			lineageSelection: { rootId: 'root-1', assetId: 'asset-1' }
		}
	});
	assert.equal(invalidIds.gridAnchor, null);
	assert.equal(invalidIds.gridSelectionId, null);
	assert.equal(invalidIds.lineageSelection, null);
	assert.deepEqual(
		parseViewPrefs('metrics', {
			version: 2,
			section: 'metrics',
			value: { tab: 'benchmarks', usageRange: '24h', mineRange: '7d' }
		}),
		{
			tab: 'usage',
			usageRange: '5m',
			mineRange: '30d',
			selectedSessionId: null
		}
	);
});

test('corrupt, oversized and unavailable sections do not break another section', () => {
	const storage = new MemoryStorage();
	writeViewPrefs(
		a,
		'metrics',
		{ tab: 'benchmarks', usageRange: '24h', mineRange: '7d', selectedSessionId: 'session-1' },
		storage
	);
	storage.setItem(
		viewPrefsKey(a, 'images'),
		JSON.stringify({
			version: 1,
			section: 'images',
			value: { mode: 'grid', search: 'x'.repeat(60 * 1024) }
		})
	);
	assert.equal((readViewPrefs(a, 'metrics', storage) as { tab: string }).tab, 'benchmarks');
	assert.equal((readViewPrefs(a, 'images', storage) as { mode: string }).mode, 'lineage');
	storage.setItem(viewPrefsKey(a, 'images'), '{');
	assert.equal((readViewPrefs(a, 'metrics', storage) as { tab: string }).tab, 'benchmarks');
});

test('storage access failures and quota errors stay contained', () => {
	const throwingStorage: PrefStorage = {
		get length(): number {
			throw new Error('blocked storage');
		},
		key() {
			throw new Error('blocked storage');
		},
		getItem() {
			throw new Error('blocked storage');
		},
		setItem() {
			throw new DOMException('quota', 'QuotaExceededError');
		},
		removeItem() {
			throw new Error('blocked storage');
		}
	};
	assert.doesNotThrow(() => readViewPrefs(a, 'metrics', throwingStorage));
	assert.doesNotThrow(() =>
		writeViewPrefs(
			a,
			'metrics',
			{ tab: 'mine', usageRange: '24h', mineRange: '7d', selectedSessionId: null },
			throwingStorage
		)
	);
	assert.doesNotThrow(() => activateViewPrefsOwner(a, 10, throwingStorage));

	const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
	Object.defineProperty(globalThis, 'localStorage', {
		configurable: true,
		get() {
			throw new Error('blocked localStorage getter');
		}
	});
	try {
		assert.doesNotThrow(() => readViewPrefs(a, 'metrics'));
		assert.doesNotThrow(() => writeViewPrefs(a, 'metrics', defaultViewPrefs('metrics')));
	} finally {
		if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor);
		else delete (globalThis as { localStorage?: Storage }).localStorage;
	}
});

test('unchanged preferences do not write storage again', () => {
	class CountingStorage extends MemoryStorage {
		writes = 0;
		setItem(key: string, value: string) {
			this.writes += 1;
			super.setItem(key, value);
		}
	}
	const storage = new CountingStorage();
	const value = {
		tab: 'mine' as const,
		usageRange: '24h' as const,
		mineRange: '7d' as const,
		selectedSessionId: null
	};
	writeViewPrefs(a, 'metrics', value, storage);
	const firstWriteCount = storage.writes;
	writeViewPrefs(a, 'metrics', value, storage);
	assert.equal(storage.writes, firstWriteCount);
});

test('account profile activation evicts the oldest profile but retains eight', () => {
	const storage = new MemoryStorage();
	for (let i = 0; i < 9; i++) {
		const owner: ViewOwner = {
			kind: 'account',
			id: `550e8400-e29b-41d4-a716-${String(i).padStart(12, '0')}`
		};
		writeViewPrefs(
			owner,
			'metrics',
			{ tab: 'mine', usageRange: '5m', mineRange: '30d', selectedSessionId: null },
			storage
		);
		activateViewPrefsOwner(owner, i + 1, storage);
	}
	const first: ViewOwner = { kind: 'account', id: '550e8400-e29b-41d4-a716-000000000000' };
	assert.equal(hasViewPrefs(first, 'metrics', storage), false);
	assert.equal(storage.length, 9);
});

test('profile activation keeps the current saved owner when old profile times are in the future', () => {
	const storage = new MemoryStorage();
	const ids = Array.from(
		{ length: 8 },
		(_, i) => `550e8400-e29b-41d4-a716-${String(i + 20).padStart(12, '0')}`
	);
	for (const id of ids) {
		writeViewPrefs(
			{ kind: 'account', id },
			'metrics',
			{ tab: 'mine', usageRange: '24h', mineRange: '7d', selectedSessionId: null },
			storage
		);
	}
	const currentId = '550e8400-e29b-41d4-a716-000000000099';
	const currentOwner: ViewOwner = { kind: 'account', id: currentId };
	writeViewPrefs(
		currentOwner,
		'metrics',
		{ tab: 'benchmarks', usageRange: '1h', mineRange: '30d', selectedSessionId: 'saved-session' },
		storage
	);
	storage.setItem(
		'potocolom-view-v1:profiles',
		JSON.stringify(ids.map((id, index) => ({ id: `account-${id}`, at: 1_000_000 + index })))
	);

	activateViewPrefsOwner(currentOwner, 50, storage);

	assert.equal(hasViewPrefs(currentOwner, 'metrics', storage), true);
	assert.equal(readViewPrefs(currentOwner, 'metrics', storage).tab, 'benchmarks');
	assert.equal(hasViewPrefs({ kind: 'account', id: ids[0] }, 'metrics', storage), false);
	for (const id of ids.slice(1)) {
		assert.equal(hasViewPrefs({ kind: 'account', id }, 'metrics', storage), true);
	}
	const index = JSON.parse(storage.getItem('potocolom-view-v1:profiles') ?? '[]') as {
		id: string;
		at: number;
	}[];
	assert.equal(index.length, 8);
	assert.ok(index.some((profile) => profile.id === `account-${currentId}` && profile.at === 50));
});

test('invalid Images mode falls back to the default while explicit modes remain valid', () => {
	const parseMode = (mode: unknown) =>
		parseViewPrefs('images', { version: 1, section: 'images', value: { mode } }).mode;
	assert.equal(parseMode('invalid'), defaultViewPrefs('images').mode);
	assert.equal(parseMode(undefined), defaultViewPrefs('images').mode);
	assert.equal(parseMode('grid'), 'grid');
	assert.equal(parseMode('lineage'), 'lineage');
});

test('missing or corrupt profile indexes reconcile stored profiles before eviction', () => {
	for (const indexValue of [null, '{broken']) {
		const storage = new MemoryStorage();
		const ids = Array.from(
			{ length: 8 },
			(_, i) => `550e8400-e29b-41d4-a716-${String(i).padStart(12, '0')}`
		);
		for (const id of ids) storage.setItem(`potocolom-view-v1:account-${id}:metrics`, '{}');
		if (indexValue !== null) storage.setItem('potocolom-view-v1:profiles', indexValue);
		const nextId = '550e8400-e29b-41d4-a716-999999999999';
		activateViewPrefsOwner({ kind: 'account', id: nextId }, 50, storage);
		assert.equal(storage.getItem(`potocolom-view-v1:account-${ids[0]}:metrics`), null);
		for (const id of ids.slice(1))
			assert.notEqual(storage.getItem(`potocolom-view-v1:account-${id}:metrics`), null);
		const index = JSON.parse(storage.getItem('potocolom-view-v1:profiles') ?? '[]') as {
			id: string;
			at: number;
		}[];
		assert.equal(index.length, 8);
		assert.ok(index.some((profile) => profile.id === `account-${nextId}` && profile.at === 50));
	}
});
