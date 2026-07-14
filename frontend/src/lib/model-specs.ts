/** Static model characteristics for the benchmark comparison table. */
export type ModelSpec = {
	id: string;
	name: string;
	architecture: string;
	parameters: string;
	min_vram_gb: number;
	resolutions: string;
	step_range: string;
	capabilities: string[];
	license: string;
	commercial: string;
	/** Offered in the studio UI (not benchmark-only). */
	studio: boolean;
	source: string;
};

export const MODEL_SPECS: ModelSpec[] = [
	{
		id: 'sdxl-base',
		name: 'SDXL (default)',
		architecture: 'SDXL',
		parameters: '~3.5B',
		min_vram_gb: 10,
		resolutions: '768, 1024',
		step_range: '10-50',
		capabilities: ['text_to_image', 'image_to_image'],
		license: 'CreativeML Open RAIL++-M',
		commercial: 'Unrestricted (RAIL use policy)',
		studio: true,
		source: 'stabilityai/stable-diffusion-xl-base-1.0'
	},
	{
		id: 'sdxl-fast',
		name: 'SDXL Fast',
		architecture: 'SDXL + Lightning LoRA',
		parameters: '~3.5B',
		min_vram_gb: 10,
		resolutions: '1024',
		step_range: '4-8',
		capabilities: ['text_to_image'],
		license: 'RAIL++-M + ByteDance Lightning',
		commercial: 'Unrestricted (RAIL use policy)',
		studio: true,
		source: 'stabilityai/stable-diffusion-xl-base-1.0'
	},
	{
		id: 'ssd-1b',
		name: 'SSD-1B',
		architecture: 'SDXL distilled',
		parameters: '~1.3B',
		min_vram_gb: 8,
		resolutions: '768, 1024',
		step_range: '10-40',
		capabilities: ['text_to_image'],
		license: 'Apache 2.0',
		commercial: 'Unrestricted',
		studio: true,
		source: 'segmind/SSD-1B'
	},
	{
		id: 'dreamshaper-lcm',
		name: 'DreamShaper 8 LCM',
		architecture: 'SD 1.5 LCM',
		parameters: '~860M',
		min_vram_gb: 6,
		resolutions: '512, 768',
		step_range: '2-15',
		capabilities: ['text_to_image'],
		license: 'CreativeML Open RAIL-M',
		commercial: 'Unrestricted (RAIL use policy)',
		studio: true,
		source: 'lykon/dreamshaper-8-lcm'
	},
	{
		id: 'sd-turbo',
		name: 'SD Turbo',
		architecture: 'SD 1.5 distilled',
		parameters: '~860M',
		min_vram_gb: 8,
		resolutions: '512',
		step_range: '1-4',
		capabilities: ['text_to_image', 'image_to_image', 'realtime'],
		license: 'Stability AI Community',
		commercial: '≤ $1M revenue / year',
		studio: false,
		source: 'stabilityai/sd-turbo'
	},
	{
		id: 'sdxl-turbo',
		name: 'SDXL Turbo',
		architecture: 'SDXL distilled',
		parameters: '~3.5B',
		min_vram_gb: 10,
		resolutions: '512',
		step_range: '1-4',
		capabilities: ['text_to_image', 'image_to_image', 'realtime'],
		license: 'Stability AI Community',
		commercial: '≤ $1M revenue / year',
		studio: false,
		source: 'stabilityai/sdxl-turbo'
	}
];

export function modelSpec(id: string): ModelSpec | undefined {
	return MODEL_SPECS.find((row) => row.id === id);
}

export function formatCapabilities(caps: string[]): string {
	return caps.map((c) => c.replace(/_/g, ' ')).join(', ');
}
