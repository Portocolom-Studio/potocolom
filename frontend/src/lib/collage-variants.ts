export type MasonryCollageConfig = {
	title: string;
	columnsClass: string;
	columnGap: string;
	tileMargin: string;
	radius: string;
};

export const masonryCollageVariants = {
	masonry: {
		title: 'Masonry responsive',
		columnsClass: 'columns-2 sm:columns-3 lg:columns-4',
		columnGap: 'gap-3',
		tileMargin: 'mb-3',
		radius: 'rounded-xl'
	},
	'masonry-2col': {
		title: 'Masonry 2 column',
		columnsClass: 'columns-2',
		columnGap: 'gap-4',
		tileMargin: 'mb-4',
		radius: 'rounded-xl'
	},
	'masonry-3col': {
		title: 'Masonry 3 column',
		columnsClass: 'columns-3',
		columnGap: 'gap-3',
		tileMargin: 'mb-3',
		radius: 'rounded-xl'
	},
	'masonry-4col': {
		title: 'Masonry 4 column',
		columnsClass: 'columns-2 sm:columns-4',
		columnGap: 'gap-3',
		tileMargin: 'mb-3',
		radius: 'rounded-lg'
	},
	'masonry-tight': {
		title: 'Masonry tight',
		columnsClass: 'columns-2 sm:columns-3 lg:columns-5',
		columnGap: 'gap-2',
		tileMargin: 'mb-2',
		radius: 'rounded-lg'
	},
	'masonry-loose': {
		title: 'Masonry loose',
		columnsClass: 'columns-1 sm:columns-2 lg:columns-3',
		columnGap: 'gap-6',
		tileMargin: 'mb-6',
		radius: 'rounded-2xl'
	},
	'masonry-editorial': {
		title: 'Masonry editorial',
		columnsClass: 'columns-1 sm:columns-2',
		columnGap: 'gap-5',
		tileMargin: 'mb-5',
		radius: 'rounded-2xl'
	}
} satisfies Record<string, MasonryCollageConfig>;

export const collagePreviewVariants = {
	...masonryCollageVariants,
	graph: {
		title: 'Graph layout',
		kind: 'graph' as const
	}
} as const;

export type CollagePreviewVariantId = keyof typeof collagePreviewVariants;

export const collagePreviewList = Object.entries(collagePreviewVariants).map(
	([id, variant], index) => ({
		id: id as CollagePreviewVariantId,
		title: variant.title,
		port: 5190 + index
	})
);
