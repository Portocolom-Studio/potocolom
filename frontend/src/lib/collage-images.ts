export type CollageImage = {
	file: string;
	src: string;
	alt: string;
	width: number;
	height: number;
};

type CollageTileTier = 'hero' | 'large' | 'medium' | 'small' | 'tiny';

const image = (file: string, alt: string, width: number, height: number): CollageImage => ({
	file,
	src: `/images/${file.replace(/ /g, '%20')}`,
	alt,
	width,
	height
});

/** URL slug for generated landing WebP variants (see scripts/generate-gallery-images.mjs). */
export function collageLandingSlug(file: string): string {
	return file.replace(/\.[^.]+$/, '').replace(/\s+/g, '-');
}

export function collageLandingSources(image: CollageImage) {
	const slug = collageLandingSlug(image.file);
	return {
		src: `/images/gallery/${slug}-480.webp`,
		srcset: `/images/gallery/${slug}-320.webp 320w, /images/gallery/${slug}-480.webp 480w`
	};
}

/** Full catalog of gallery-eligible images (excludes branding assets). */
const collageCatalog: CollageImage[] = [
	image('squirrel.jpg', 'Squirrel in the forest', 5184, 3456),
	image('mountian.jpg', 'Mountain landscape', 3840, 2160),
	image('cat.jpg', 'Cat portrait scene', 2560, 1440),
	image('jqbLhzV7.jpeg', 'Generative portrait', 2560, 2560),
	image('fox.png', 'Fox illustration', 1024, 1536),
	image('relaxed_minion.png', 'Relaxed minion', 1024, 1536),
	image('mountain_chibbi.png', 'Mountain chibi scene', 1024, 1024),
	image('makako.jpeg', 'Makako study', 1083, 1185),
	image('Screenshot from 2025-01-09 14-15-58.jpg', 'Studio screenshot', 928, 1232),
	image('063f8ee387f1ef41889b695fec895e98-4212568279.jpg', 'Character render', 736, 714),
	image('9VtTGpS8.jpeg', 'Abstract study', 640, 640),
	image('abstract_cat.png', 'Abstract cat', 640, 640),
	image('output.png', 'Model output', 640, 640),
	image('abstract_man.jpg', 'Abstract figure', 600, 448),
	image('flow.jpg', 'Flow study', 600, 418),
	image('41UpghU7P1L-3126336320.jpg', 'Book cover', 469, 500),
	image('aboriginal-art-2.jpg', 'Aboriginal art study', 1024, 1024),
	image('aboriginal-art-.jpg', 'Aboriginal dot painting', 1280, 960),
	image(
		'david-goggins-in-shades-8f73y0dmfi8nlxjs-3216685373.jpg',
		'Portrait in shades',
		1920,
		1080
	),
	image('fujistu.jpg', 'Fujitsu illustration', 720, 720),
	image('merlin.jpg', 'Merlin character scene', 2048, 1365),
	image('flying-feathers.jpg', 'Flying feathers abstract', 2560, 1417),
	image('relativity-clock-painting.jpg', 'Relativity clock painting', 736, 981),
	image(
		'48c97eed-cf29-487d-b4b7-4553dcd4e5ff-profile_image-300x300.png',
		'Profile avatar',
		300,
		300
	)
];

const catalogByFile = new Map(collageCatalog.map((entry) => [entry.file, entry]));

function pick(file: string): CollageImage {
	const entry = catalogByFile.get(file);
	if (!entry) throw new Error(`Missing collage image: ${file}`);
	return entry;
}

/**
 * Curated grid order: heroes spaced by smaller tiles so dense packing fills
 * each row across the full 6-column track.
 */
export const collageImages: CollageImage[] = [
	pick('squirrel.jpg'),
	pick('aboriginal-art-2.jpg'),
	pick('merlin.jpg'),
	pick('48c97eed-cf29-487d-b4b7-4553dcd4e5ff-profile_image-300x300.png'),
	pick('fox.png'),
	pick('flow.jpg'),
	pick('abstract_cat.png'),
	pick('fujistu.jpg'),
	pick('david-goggins-in-shades-8f73y0dmfi8nlxjs-3216685373.jpg'),
	pick('9VtTGpS8.jpeg'),
	pick('mountain_chibbi.png'),
	pick('makako.jpeg'),
	pick('aboriginal-art-.jpg'),
	pick('063f8ee387f1ef41889b695fec895e98-4212568279.jpg'),
	pick('cat.jpg'),
	pick('jqbLhzV7.jpeg'),
	pick('Screenshot from 2025-01-09 14-15-58.jpg'),
	pick('abstract_man.jpg'),
	pick('41UpghU7P1L-3126336320.jpg'),
	pick('output.png'),
	pick('flying-feathers.jpg'),
	pick('relativity-clock-painting.jpg'),
	pick('relaxed_minion.png'),
	pick('mountian.jpg')
];

export function collageImage(file: string): CollageImage {
	return pick(file);
}

function megapixels(image: CollageImage) {
	return (image.width * image.height) / 1_000_000;
}

function aspectRatio(image: CollageImage) {
	return image.width / image.height;
}

function collageTileTier(image: CollageImage): CollageTileTier {
	const mp = megapixels(image);
	const aspect = aspectRatio(image);
	const longEdge = Math.max(image.width, image.height);

	if (aspect >= 1.15 && mp >= 3) return 'hero';
	if (aspect >= 1.2 && mp >= 1.4) return 'hero';

	if (mp >= 4 && aspect >= 0.85 && aspect <= 1.15) return 'large';
	if (longEdge < 520 || mp < 0.35) return 'tiny';
	if (longEdge < 700 || mp < 0.65) return 'small';
	if (mp < 1.35) return 'medium';

	return 'medium';
}

const tileClassByTier: Record<CollageTileTier, string> = {
	hero: '[column-span:all]',
	large: 'mx-auto w-[94%]',
	medium: '',
	small: 'mx-auto w-[74%]',
	tiny: 'mx-auto w-[56%]'
};

/** Tile sizing from resolution and orientation for column masonry previews. */
export function collageTileClass(image: CollageImage): string {
	return tileClassByTier[collageTileTier(image)];
}

/** Landing gallery: shrink only tiny tiles so masonry packs evenly. */
export function collageLandingTileClass(image: CollageImage): string {
	const tier = collageTileTier(image);

	if (tier === 'tiny') return 'mx-auto w-[72%]';
	if (tier === 'small') return 'mx-auto w-[88%]';
	return '';
}

/** Grid column span for the landing gallery (dense grid, full width). */
export function collageGridClass(image: CollageImage): string {
	const tier = collageTileTier(image);
	const aspect = aspectRatio(image);

	if (tier === 'hero') return 'col-span-4 sm:col-span-6';

	if (tier === 'large') {
		return aspect >= 1 ? 'col-span-3 sm:col-span-4' : 'col-span-2 sm:col-span-3';
	}

	if (aspect >= 1.15) return 'col-span-2 sm:col-span-3';
	if (aspect <= 0.82) return 'col-span-1 sm:col-span-2';

	return 'col-span-1 sm:col-span-2';
}
