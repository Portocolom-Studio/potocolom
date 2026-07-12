// Shared studio state: the sidebar (model list, gallery) and the generate
// panel look at the same registry and history.

export type Model = {
	id: string;
	name: string;
	capabilities: string[];
	default: boolean;
	parameters: {
		properties?: Record<string, { enum?: number[]; default?: unknown }>;
	} & Record<string, unknown>;
};

export type Asset = { id: string; url: string; width: number; height: number };

export type Generation = {
	id: string;
	model_id: string;
	params: { prompt?: string } & Record<string, unknown>;
	state: string;
	progress: number | null; // denoising fraction while running, else null
	gpu_ms: number | null;
	created_at: string;
	assets: Asset[];
};

export const studio = $state({
	models: [] as Model[],
	modelId: '',
	prompt: '',
	selectedId: null as string | null, // generation pinned in the viewer
	history: [] as Generation[]
});

let polling = false;

export async function loadModels(): Promise<void> {
	const response = await fetch('/api/v1/models');
	if (!response.ok) return;
	studio.models = (await response.json()) as Model[];
	if (!studio.modelId && studio.models.length > 0) {
		studio.modelId = (studio.models.find((m) => m.default) ?? studio.models[0]).id;
	}
}

export async function loadHistory(): Promise<void> {
	const response = await fetch('/api/v1/generations');
	if (!response.ok) return; // degraded API: history needs the database
	studio.history = (await response.json()) as Generation[];
}

export async function pollWhileWorking(): Promise<void> {
	if (polling) return;
	polling = true;
	try {
		while (studio.history.some((g) => g.state === 'queued' || g.state === 'running')) {
			await new Promise((resolve) => setTimeout(resolve, 1500));
			await loadHistory();
		}
	} finally {
		polling = false;
	}
}
