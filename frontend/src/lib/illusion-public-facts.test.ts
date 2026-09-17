import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import {
	fillIllusionCopy,
	ILLUSION_CANDIDATES,
	ILLUSION_COPY_VARS,
	ILLUSION_GALLERY,
	ILLUSION_HERO,
	ILLUSION_HERO_ID
} from './illusion-public-facts.ts';

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, '../../static');

test('gallery keepers are unique score-4-or-5 exports', () => {
	assert.equal(ILLUSION_HERO.id, ILLUSION_HERO_ID);
	const ids = ILLUSION_GALLERY.map((item) => item.id);
	assert.equal(new Set(ids).size, ids.length);
	const viewShas = ILLUSION_GALLERY.map((item) => item.viewSha256);
	const primeShas = ILLUSION_GALLERY.map((item) => item.primeSha256);
	assert.equal(new Set(viewShas).size, viewShas.length);
	assert.equal(new Set(primeShas).size, primeShas.length);
	for (const item of ILLUSION_GALLERY) {
		assert.ok(item.score === 4 || item.score === 5, item.id);
		assert.ok(item.frame === 'none' || item.frame === 'minor');
		assert.ok(existsSync(join(staticDir, item.view.slice(1))));
		assert.ok(existsSync(join(staticDir, item.prime.slice(1))));
	}
});

test('candidate tray items exist with provenance', () => {
	assert.ok(ILLUSION_CANDIDATES.length > 0);
	const ids = [
		...ILLUSION_GALLERY.map((item) => item.id),
		...ILLUSION_CANDIDATES.map((item) => item.id)
	];
	assert.equal(new Set(ids).size, ids.length);
	for (const item of ILLUSION_CANDIDATES) {
		assert.ok(item.score === 4 || item.score === 5, item.id);
		assert.ok(item.frame === 'none' || item.frame === 'minor' || item.frame === 'unrated');
		assert.ok(existsSync(join(staticDir, item.view.slice(1))), item.id);
		assert.ok(existsSync(join(staticDir, item.prime.slice(1))), item.id);
	}
});

test('public copy placeholders resolve to measured numbers', () => {
	const jointCount = ILLUSION_GALLERY.filter((item) => item.mode === 'joint').length;
	const filled = fillIllusionCopy(
		'{auc} {bar} {codeSds} {codeDream} {researchSds} {researchDream} {cost} {adamWithout} {adamLow} {adamHigh} {adamSds} {adamDream} {adamDreamSteps} {export} {galleryCount} {jointCount} {score} {minScore}'
	);
	assert.equal(
		filled,
		`0.706 0.75 500 8 5000 1 3.3 2 44 60 250 4 150 window2-2026-08-clean ${ILLUSION_GALLERY.length} ${jointCount} ${ILLUSION_GALLERY[0].score} 4`
	);
	assert.equal(fillIllusionCopy('keep {unknown}'), 'keep {unknown}');
	assert.equal(ILLUSION_COPY_VARS.galleryCount, String(ILLUSION_GALLERY.length));
	assert.equal(ILLUSION_COPY_VARS.jointCount, String(jointCount));
	assert.equal(ILLUSION_COPY_VARS.score, String(ILLUSION_GALLERY[0].score));
});

test('English and Spanish dictionaries share keys, and ill strings use known slots', () => {
	const en = JSON.parse(readFileSync(join(here, 'i18n/en.json'), 'utf8')) as Record<string, string>;
	const es = JSON.parse(readFileSync(join(here, 'i18n/es.json'), 'utf8')) as Record<string, string>;
	assert.deepEqual(Object.keys(en).sort(), Object.keys(es).sort());
	const slot = /\{(\w+)\}/g;
	for (const [key, value] of Object.entries(en)) {
		if (!key.startsWith('ill.')) continue;
		for (const match of value.matchAll(slot)) {
			assert.ok(match[1] in ILLUSION_COPY_VARS, `${key} has unknown placeholder {${match[1]}}`);
		}
	}
	assert.match(en['ill.s5_p2'], /optimizer CLI defaults/);
	assert.match(es['ill.s5_p2'], /valores por defecto del CLI/);
	assert.doesNotMatch(en['ill.s5_p2'], /shipped/i);
	assert.doesNotMatch(es['ill.s5_p2'], /publicado/i);
	assert.match(en['ill.s5_p3'], /\{adamSds\}/);
	assert.match(en['ill.s7_p4'], /\{galleryCount\}/);
	assert.match(en['ill.s7_p4'], /\{jointCount\}/);
	assert.match(en['ill.s7_p4'], /after the gallery was baked/);
	assert.match(en['ill.s8_p1'], /\{galleryCount\}/);
	assert.match(en['ill.s8_p1'], /\{score\}/);
});

test('the public illusions route is wired', () => {
	const sitemap = readFileSync(join(here, '../routes/sitemap.xml/+server.ts'), 'utf8');
	const shell = readFileSync(join(here, 'components/landing/LatentShell.svelte'), 'utf8');
	const orbit = readFileSync(join(here, 'components/landing/SketchOrbit.svelte'), 'utf8');
	const page = readFileSync(join(here, '../routes/illusions/+page.svelte'), 'utf8');
	assert.match(sitemap, /\/illusions/);
	assert.match(shell, /current === 'illusions'/);
	assert.match(orbit, /resolve\('\/illusions'\)/);
	assert.match(page, /LatentShell current="illusions"/);
	assert.match(page, /rotate\(180deg\)|IllusionFlip/);
	assert.match(page, /fig-scroll/);
});

test('study diagrams are exported as webp', () => {
	const names = [
		'architecture',
		'workflow',
		'ffn',
		'sds',
		'symbols',
		'two-phase',
		'dream',
		'joint',
		'recipe',
		'review',
		'seeds',
		'failures',
		'print'
	];
	for (const name of names) {
		assert.ok(existsSync(join(staticDir, 'illusions', `${name}.webp`)), name);
	}
});
