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
		id: 'sdxl-hypersd',
		name: 'SDXL Hyper-SD',
		architecture: 'SDXL + Hyper-SD LoRA',
		parameters: '~3.5B',
		min_vram_gb: 10,
		resolutions: '1024',
		step_range: '4-8',
		capabilities: ['text_to_image'],
		license: 'RAIL++-M base; LoRA: CreativeML Open RAIL++-M',
		commercial: 'Unrestricted (RAIL use policy)',
		studio: false,
		source: 'stabilityai/stable-diffusion-xl-base-1.0'
	},
	{
		id: 'ssd-1b-lightning',
		name: 'SSD-1B + Lightning',
		architecture: 'SSD-1B + Lightning LoRA',
		parameters: '~1.3B',
		min_vram_gb: 8,
		resolutions: '1024',
		step_range: '4-8',
		capabilities: ['text_to_image'],
		license: 'Apache 2.0 base + Open RAIL++-M LoRA',
		commercial: 'RAIL use policy applies',
		studio: true,
		source: 'segmind/SSD-1B'
	},
	{
		id: 'sd35-medium',
		name: 'Stable Diffusion 3.5 Medium',
		architecture: 'MMDiT + CLIP-L + OpenCLIP-G + T5-XXL',
		parameters: '~2.5B',
		min_vram_gb: 14,
		resolutions: '768, 1024',
		step_range: '20-50',
		capabilities: ['text_to_image'],
		license: 'Stability AI Community',
		commercial: '<= $1M revenue / year',
		studio: true,
		source: 'stabilityai/stable-diffusion-3.5-medium'
	},
	{
		id: 'flux2-klein-4b',
		name: 'FLUX.2 Klein 4B',
		architecture: 'FLUX.2 distilled',
		parameters: '~4B generator',
		min_vram_gb: 13,
		resolutions: '512, 768, 1024',
		step_range: '1-8',
		capabilities: ['text_to_image'],
		license: 'Apache 2.0',
		commercial: 'Commercial hosting allowed; license and notice duties apply',
		studio: false,
		source: 'black-forest-labs/FLUX.2-klein-4B'
	},
	{
		id: 'z-image-turbo',
		name: 'Z-Image Turbo',
		architecture: 'Z-Image distilled',
		parameters: '~6B generator',
		min_vram_gb: 20,
		resolutions: '512, 768, 1024',
		step_range: '4-8',
		capabilities: ['text_to_image', 'image_to_image'],
		license: 'Apache 2.0',
		commercial: 'Commercial hosting allowed; license and notice duties apply',
		studio: false,
		source: 'Tongyi-MAI/Z-Image-Turbo'
	},
	{
		id: 'moss-image-sft',
		name: 'MOSS-Image SFT',
		architecture: 'MOSS DiT + Qwen3.5-9B',
		parameters: '5B generator + 9B encoder',
		min_vram_gb: 32,
		resolutions: '512, 768, 1024',
		step_range: '10-100',
		capabilities: ['text_to_image'],
		license: 'Apache 2.0',
		commercial: 'Commercial hosting allowed; license and notice duties apply',
		studio: false,
		source: 'OpenMOSS-Team/MOSS-Image-SFT'
	},
	{
		id: 'kromoss',
		name: 'Kromoss',
		architecture: 'MOSS fine-tune + Qwen3.5-9B',
		parameters: '5B generator + 9B encoder',
		min_vram_gb: 32,
		resolutions: '512, 768, 1024',
		step_range: '10-100',
		capabilities: ['text_to_image'],
		license: 'Apache 2.0',
		commercial: 'Commercial hosting allowed; license and notice duties apply',
		studio: false,
		source: 'lodestones/Kromoss'
	},
	{
		id: 'iris-3b',
		name: 'Iris-3B',
		architecture: 'Pixel-space DiT + Qwen3-VL-4B',
		parameters: '3B generator + 4B encoder',
		min_vram_gb: 22,
		resolutions: '512, 768, 1024',
		step_range: '10-100',
		capabilities: ['text_to_image'],
		license: 'Apache 2.0',
		commercial: 'Commercial hosting allowed; license and notice duties apply',
		studio: false,
		source: 'speridlabs/iris-3b'
	},
	{
		id: 'flux3-klein',
		name: 'FLUX 3 Image Klein',
		architecture: 'FLUX 3 distilled + Qwen3-VL-4B',
		parameters: '5.6B generator + 4B encoder',
		min_vram_gb: 32,
		resolutions: '512, 768, 1024',
		step_range: '16-16',
		capabilities: ['text_to_image'],
		license: 'FLUX Kommunity v1.0',
		commercial:
			'Commercial outputs below $5M annualized affiliate revenue; filtering or review required',
		studio: false,
		source: 'black-forest-labs/flux-3-image-klein'
	}
];

export function modelSpec(id: string): ModelSpec | undefined {
	return MODEL_SPECS.find((row) => row.id === id);
}

export function formatCapabilities(caps: string[]): string {
	return caps.map((c) => c.replace(/_/g, ' ')).join(', ');
}
