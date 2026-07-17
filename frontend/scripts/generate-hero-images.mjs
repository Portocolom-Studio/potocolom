#!/usr/bin/env node
/**
 * Build the hero image field set: gallery catalog + sdxl-base high-guidance
 * renders (benchmark run 20260715-064718 and hero-batch) + sdxl base/fast
 * second takes of the hero-batch prompts from the hero-* quick runs
 * (suffixed -alt so both renders of a prompt can coexist).
 * Sources: frontend/static/images/gallery/<slug>-320.webp (already generated)
 *          data/benchmark/20260715-064718/images/<prompt>/sdxl-base__1024-high-guidance__1024x1024-s20.webp
 *          data/hero-batch/<prompt>/sdxl-base__1024-high-guidance__1024x1024-s20.webp
 *          data/benchmark/hero-<run>-quick-<stamp>/images/<prompt>/sdxl-base__1024-default (or sdxl-fast__1024-8step)
 * Output:  frontend/static/images/hero/<slug>-160.webp  (default tile, 1x DPR)
 *          frontend/static/images/hero/<slug>-320.webp  (magnified / 2x DPR)
 *          frontend/src/lib/hero-images.json (manifest the component imports;
 *          entries are the -320 filenames; the component derives -160)
 *
 * data/ is gitignored: the sources only exist on machines that ran the
 * benchmark / hero batch. The generated thumbs and manifest are committed, so
 * this script only needs re-running when the image set changes.
 *
 * Hero batch: backend/.venv/bin/python scripts/generate-hero-batch.py
 */
import { copyFile, mkdir, readdir, stat, writeFile } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const __dirname = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(__dirname, '..');
const galleryDir = join(frontendRoot, 'static', 'images', 'gallery');
const heroDir = join(frontendRoot, 'static', 'images', 'hero');
const manifestPath = join(frontendRoot, 'src', 'lib', 'hero-images.json');
const repoRoot = join(frontendRoot, '..');
const highGuidance = ['sdxl-base__1024-high-guidance__1024x1024-s20.webp'];
// The quick-run comparison winner, with the 8-step render as fallback.
const baseOrFast = [
	'sdxl-base__1024-default__1024x1024-s20.webp',
	'sdxl-fast__1024-8step__1024x1024-s8.webp'
];
const sdxlSources = [
	{
		dir: join(repoRoot, 'data', 'benchmark', '20260715-064718', 'images'),
		variants: highGuidance,
		suffix: ''
	},
	{ dir: join(repoRoot, 'data', 'hero-batch'), variants: highGuidance, suffix: '' },
	{
		dir: join(repoRoot, 'data', 'benchmark', 'hero-20-quick-20260717-160232', 'images'),
		variants: baseOrFast,
		suffix: '-alt'
	},
	{
		dir: join(repoRoot, 'data', 'benchmark', 'hero-20b-quick-20260717-201805', 'images'),
		variants: baseOrFast,
		suffix: '-alt'
	},
	{
		dir: join(repoRoot, 'data', 'benchmark', 'hero-32c-quick-20260717-203457', 'images'),
		variants: baseOrFast,
		suffix: '-alt'
	}
];

/** Display size (~116 CSS px) and magnified/2x companion. */
const variants = [
	{ width: 160, quality: 75 },
	{ width: 320, quality: 82 }
];
const manifestWidth = 320;

/** Keep in sync with collageCatalog in src/lib/collage-images.ts */
const gallerySlugs = [
	'squirrel',
	'mountian',
	'cat',
	'jqbLhzV7',
	'fox',
	'relaxed_minion',
	'mountain_chibbi',
	'makako',
	'Screenshot-from-2025-01-09-14-15-58',
	'063f8ee387f1ef41889b695fec895e98-4212568279',
	'9VtTGpS8',
	'abstract_cat',
	'output',
	'abstract_man',
	'flow',
	'41UpghU7P1L-3126336320',
	'aboriginal-art-2',
	'aboriginal-art-',
	'david-goggins-in-shades-8f73y0dmfi8nlxjs-3216685373',
	'fujistu',
	'merlin',
	'flying-feathers',
	'relativity-clock-painting',
	'48c97eed-cf29-487d-b4b7-4553dcd4e5ff-profile_image-300x300'
];

async function writeVariants(inputPath, slug, manifest) {
	const manifestFile = `${slug}-${manifestWidth}.webp`;
	if (manifest.includes(manifestFile)) throw new Error(`Slug collision: ${manifestFile}`);
	for (const { width, quality } of variants) {
		const file = `${slug}-${width}.webp`;
		const buffer = await sharp(inputPath)
			.resize({ width, withoutEnlargement: true })
			.webp({ quality, effort: 4 })
			.toBuffer();
		await sharp(buffer).toFile(join(heroDir, file));
		console.log(`Wrote ${file} (${buffer.length} bytes)`);
	}
	manifest.push(manifestFile);
}

async function main() {
	await mkdir(heroDir, { recursive: true });
	const manifest = [];

	for (const slug of gallerySlugs) {
		const gallery320 = join(galleryDir, `${slug}-320.webp`);
		await copyFile(gallery320, join(heroDir, `${slug}-320.webp`));
		const buffer = await sharp(gallery320)
			.resize({ width: 160, withoutEnlargement: true })
			.webp({ quality: 75, effort: 4 })
			.toBuffer();
		await sharp(buffer).toFile(join(heroDir, `${slug}-160.webp`));
		manifest.push(`${slug}-320.webp`);
		console.log(`Copied ${slug}-320.webp; wrote ${slug}-160.webp (${buffer.length} bytes)`);
	}

	for (const source of sdxlSources) {
		let promptDirs;
		try {
			promptDirs = (await readdir(source.dir)).sort();
		} catch {
			console.warn(`Skip missing source dir: ${source.dir}`);
			continue;
		}
		for (const promptDir of promptDirs) {
			const dirPath = join(source.dir, promptDir);
			try {
				const info = await stat(dirPath);
				if (!info.isDirectory()) continue;
			} catch {
				continue;
			}
			let inputPath = null;
			for (const variant of source.variants) {
				const candidate = join(dirPath, variant);
				try {
					await stat(candidate);
					inputPath = candidate;
					break;
				} catch {
					// try the next variant
				}
			}
			if (!inputPath) {
				console.warn(`Skip missing render: ${promptDir}`);
				continue;
			}

			const slug = promptDir.replace(/^\d+-/, '') + source.suffix;
			await writeVariants(inputPath, slug, manifest);
		}
	}

	await writeFile(manifestPath, `${JSON.stringify(manifest, null, '\t')}\n`);
	console.log(`Wrote ${basename(manifestPath)} (${manifest.length} images)`);
}

main().catch((error) => {
	console.error(error);
	process.exit(1);
});
