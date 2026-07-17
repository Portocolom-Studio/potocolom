#!/usr/bin/env node
/**
 * Build the hero image field set: gallery catalog + sdxl-base high-guidance
 * renders (benchmark run 20260715-064718 and optional hero-batch).
 * Sources: frontend/static/images/gallery/<slug>-320.webp (already generated)
 *          data/benchmark/20260715-064718/images/<prompt>/sdxl-base__1024-high-guidance__1024x1024-s20.webp
 *          data/hero-batch/<prompt>/sdxl-base__1024-high-guidance__1024x1024-s20.webp
 * Output:  frontend/static/images/hero/<slug>-320.webp
 *          frontend/src/lib/hero-images.json (manifest the component imports)
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
const sdxlSources = [
	join(repoRoot, 'data', 'benchmark', '20260715-064718', 'images'),
	join(repoRoot, 'data', 'hero-batch')
];
const benchmarkVariant = 'sdxl-base__1024-high-guidance__1024x1024-s20.webp';

const width = 320;
const quality = 82;

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

async function main() {
	await mkdir(heroDir, { recursive: true });
	const manifest = [];

	for (const slug of gallerySlugs) {
		const file = `${slug}-${width}.webp`;
		await copyFile(join(galleryDir, file), join(heroDir, file));
		manifest.push(file);
		console.log(`Copied ${file}`);
	}

	for (const sourceDir of sdxlSources) {
		let promptDirs;
		try {
			promptDirs = (await readdir(sourceDir)).sort();
		} catch {
			console.warn(`Skip missing source dir: ${sourceDir}`);
			continue;
		}
		for (const promptDir of promptDirs) {
			const dirPath = join(sourceDir, promptDir);
			try {
				const info = await stat(dirPath);
				if (!info.isDirectory()) continue;
			} catch {
				continue;
			}
			const inputPath = join(dirPath, benchmarkVariant);
			try {
				await stat(inputPath);
			} catch {
				console.warn(`Skip missing render: ${promptDir}`);
				continue;
			}

			const slug = promptDir.replace(/^\d+-/, '');
			const file = `${slug}-${width}.webp`;
			if (manifest.includes(file)) throw new Error(`Slug collision: ${file}`);

			const buffer = await sharp(inputPath)
				.resize({ width, withoutEnlargement: true })
				.webp({ quality, effort: 4 })
				.toBuffer();
			await sharp(buffer).toFile(join(heroDir, file));
			manifest.push(file);
			console.log(`Wrote ${file} (${buffer.length} bytes)`);
		}
	}

	await writeFile(manifestPath, `${JSON.stringify(manifest, null, '\t')}\n`);
	console.log(`Wrote ${basename(manifestPath)} (${manifest.length} images)`);
}

main().catch((error) => {
	console.error(error);
	process.exit(1);
});
