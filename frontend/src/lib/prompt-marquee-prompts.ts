export type PromptMarqueeItem = {
	id: string;
	primary: string;
	secondary: string;
};

export function promptMarqueeText(item: PromptMarqueeItem): string {
	return `${item.primary}. ${item.secondary}`;
}

/** Sample prompts for the landing-page marquee preview. */
export const promptMarqueePrompts: PromptMarqueeItem[] = [
	{
		id: 'fox',
		primary: 'A red fox in autumn leaves',
		secondary:
			'Soft watercolor wash, amber and rust palette, loose brushwork, golden hour backlight through the canopy.'
	},
	{
		id: 'fuji',
		primary: 'Mount Fuji at sunset',
		secondary:
			'Ukiyo-e inspired illustration, indigo-to-coral gradient sky, crisp silhouette, fine linework.'
	},
	{
		id: 'cat',
		primary: 'Abstract cat made of neon lines',
		secondary:
			'Electric cyan strokes on matte black, geometric curves, glowing edges, minimal negative space.'
	},
	{
		id: 'merlin',
		primary: 'Merlin casting a spell',
		secondary:
			'Misty old-growth forest, volumetric fog, cinematic rim light, sparks of violet energy in his palm.'
	},
	{
		id: 'aboriginal',
		primary: 'Aboriginal dot painting',
		secondary:
			'River bend under a dense star field, ochre and white dots on earthen red, symmetrical composition.'
	},
	{
		id: 'minion',
		primary: 'Relaxed minion by a pool',
		secondary:
			'Photorealistic, mirrored sunglasses, turquoise water, harsh midday sun, playful vacation mood.'
	},
	{
		id: 'clocks',
		primary: 'Surrealist clocks melting',
		secondary:
			'Draped over a jagged alpine ridge at dusk, long shadows, bronze reflections, dreamlike stillness.'
	},
	{
		id: 'chibi',
		primary: 'Chibi hiker crossing snowy peaks',
		secondary:
			'Pastel palette, oversized boots, tiny canvas backpack, wind-swept snow, cheerful adventure tone.'
	},
	{
		id: 'feathers',
		primary: 'Feathers spiraling in slow motion',
		secondary:
			'Macro photograph, shallow depth of field, iridescent barbs catching sidelight, dark velvet backdrop.'
	},
	{
		id: 'squirrel',
		primary: 'Squirrel holding an acorn',
		secondary:
			'Studio portrait, warm key light, soft fur detail, neutral backdrop, curious expression, 85mm lens look.'
	},
	{
		id: 'escher',
		primary: 'Escher staircases floating',
		secondary:
			'Zero gravity marble architecture, impossible geometry, pale fog below, precise perspective lines.'
	},
	{
		id: 'scifi',
		primary: 'Sci-fi book cover',
		secondary:
			'Bold condensed typography, teal and orange nebula, distressed grain, title stacked above a lone figure.'
	},
	{
		id: 'runner',
		primary: 'Runner in heavy rain',
		secondary:
			'Dramatic black and white, motion blur on the legs, rain streaks, streetlamp flare, high contrast.'
	},
	{
		id: 'flow',
		primary: 'Flow field visualization',
		secondary:
			'Deep purple and molten gold particle streams, smooth curves, dark background, generative art aesthetic.'
	},
	{
		id: 'makako',
		primary: 'Makako portrait study',
		secondary:
			'Charcoal on textured cotton paper, cross-hatched shadows, focused gaze, raw paper tooth visible.'
	},
	{
		id: 'koi',
		primary: 'Cyberpunk alley at night',
		secondary:
			'Neon signage reflected in wet asphalt, holographic koi fish drifting through the rain, teal and magenta.'
	}
];
