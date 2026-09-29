#!/usr/bin/env node
/**
 * Build WebP landing variants for the gallery collage.
 * Sources: frontend/static/images/* (catalog files only)
 * Output:  frontend/static/images/gallery/<slug>-{320,480}.webp
 *
 * Re-run when originals in static/images/ change.
 */
import { mkdir, stat } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import sharp from 'sharp';

const __dirname = dirname(fileURLToPath(import.meta.url));
const imagesDir = join(__dirname, '..', 'static', 'images');
const galleryDir = join(imagesDir, 'gallery');

const widths = [320, 480];
const quality = 82;

/** Keep in sync with collageCatalog in src/lib/collage-images.ts */
const catalogFiles = [
	'squirrel.webp',
	'mountian.webp',
	'cat.webp',
	'jqbLhzV7.webp',
	'fox.webp',
	'relaxed_minion.webp',
	'mountain_chibbi.webp',
	'makako.jpeg',
	'Screenshot from 2025-01-09 14-15-58.webp',
	'063f8ee387f1ef41889b695fec895e98-4212568279.jpg',
	'9VtTGpS8.webp',
	'abstract_cat.webp',
	'output.webp',
	'abstract_man.jpg',
	'flow.jpg',
	'41UpghU7P1L-3126336320.jpg',
	'aboriginal-art-2.webp',
	'aboriginal-art-.webp',
	'david-goggins-in-shades-8f73y0dmfi8nlxjs-3216685373.jpg',
	'fujistu.jpg',
	'merlin.webp',
	'flying-feathers.webp',
	'relativity-clock-painting.jpg',
	'48c97eed-cf29-487d-b4b7-4553dcd4e5ff-profile_image-300x300.png'
];

export function gallerySlug(file) {
	return file.replace(/\.[^.]+$/, '').replace(/\s+/g, '-');
}

async function generateVariant(inputPath, outputPath, width) {
	const buffer = await sharp(inputPath)
		.rotate()
		.resize({ width, withoutEnlargement: true })
		.webp({ quality, effort: 4 })
		.toBuffer();

	await sharp(buffer).toFile(outputPath);
	return buffer.length;
}

async function main() {
	await mkdir(galleryDir, { recursive: true });

	let totalBytes = 0;

	for (const file of catalogFiles) {
		const inputPath = join(imagesDir, file);
		const slug = gallerySlug(file);

		try {
			await stat(inputPath);
		} catch {
			console.warn(`Skip missing source: ${file}`);
			continue;
		}

		for (const width of widths) {
			const outputPath = join(galleryDir, `${slug}-${width}.webp`);
			const bytes = await generateVariant(inputPath, outputPath, width);
			totalBytes += bytes;
			console.log(`Wrote ${basename(outputPath)} (${bytes} bytes)`);
		}
	}

	console.log(`Gallery variants total: ${(totalBytes / 1024 / 1024).toFixed(2)} MiB`);
}

main().catch((error) => {
	console.error(error);
	process.exit(1);
});
