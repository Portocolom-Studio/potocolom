import type { Model } from '$lib/studio.svelte';

// Studio models hardcoded from worker manifests until the API is wired on this branch.
export const STUDIO_MODELS: Model[] = [
	{
		id: 'sdxl-base',
		name: 'SDXL (default)',
		capabilities: ['text_to_image'],
		default: true,
		parameters: {
			type: 'object',
			properties: {
				prompt: { type: 'string' },
				steps: { type: 'integer', minimum: 10, maximum: 50, default: 20 },
				guidance: { type: 'number', minimum: 0, maximum: 15, default: 6 },
				seed: { type: 'integer' },
				width: { type: 'integer', enum: [768, 1024], default: 1024 },
				height: { type: 'integer', enum: [768, 1024], default: 1024 }
			},
			required: ['prompt']
		}
	},
	{
		id: 'sdxl-fast',
		name: 'SDXL Fast',
		capabilities: ['text_to_image'],
		default: false,
		parameters: {
			type: 'object',
			properties: {
				prompt: { type: 'string' },
				steps: { type: 'integer', minimum: 4, maximum: 8, default: 8 },
				guidance: { type: 'number', minimum: 0, maximum: 2, default: 0 },
				seed: { type: 'integer' },
				width: { type: 'integer', enum: [1024], default: 1024 },
				height: { type: 'integer', enum: [1024], default: 1024 }
			},
			required: ['prompt']
		}
	},
	{
		id: 'ssd-1b',
		name: 'SSD-1B',
		capabilities: ['text_to_image'],
		default: false,
		parameters: {
			type: 'object',
			properties: {
				prompt: { type: 'string' },
				steps: { type: 'integer', minimum: 10, maximum: 40, default: 20 },
				guidance: { type: 'number', minimum: 0, maximum: 15, default: 7 },
				seed: { type: 'integer' },
				width: { type: 'integer', enum: [768, 1024], default: 1024 },
				height: { type: 'integer', enum: [768, 1024], default: 1024 }
			},
			required: ['prompt']
		}
	},
	{
		id: 'dreamshaper-lcm',
		name: 'DreamShaper 8 LCM',
		capabilities: ['text_to_image'],
		default: false,
		parameters: {
			type: 'object',
			properties: {
				prompt: { type: 'string' },
				steps: { type: 'integer', minimum: 2, maximum: 15, default: 8 },
				guidance: { type: 'number', minimum: 0, maximum: 3, default: 1.5 },
				seed: { type: 'integer' },
				width: { type: 'integer', enum: [512, 768], default: 512 },
				height: { type: 'integer', enum: [512, 768], default: 512 }
			},
			required: ['prompt']
		}
	}
];
