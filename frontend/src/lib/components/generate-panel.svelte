<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';

	type Model = {
		id: string;
		name: string;
		capabilities: string[];
		parameters: Record<string, unknown>;
	};
	type Asset = { id: string; url: string; width: number; height: number };
	type Generation = {
		id: string;
		model_id: string;
		params: { prompt?: string } & Record<string, unknown>;
		state: string;
		gpu_ms: number | null;
		created_at: string;
		assets: Asset[];
	};

	// Matches the Input component's field styling for the native controls it
	// does not vendor (select, textarea).
	const fieldClass =
		'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 ' +
		'placeholder:text-muted-foreground w-full min-w-0 rounded-lg border bg-transparent ' +
		'px-2.5 py-1 text-base transition-colors outline-none focus-visible:ring-3 md:text-sm ' +
		'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50';

	let models = $state<Model[]>([]);
	let modelId = $state('');
	let prompt = $state('');
	let steps = $state('');
	let guidance = $state('');
	let seed = $state('');
	let size = $state('512');
	let busy = $state(false);
	let statusKey = $state<'idle' | 'queued' | 'running' | 'failed'>('idle');
	let progress = $state<number | null>(null);
	let errorText = $state('');
	let history = $state<Generation[]>([]);

	const latest = $derived(history.find((g) => g.assets.length > 0) ?? null);

	async function loadModels(): Promise<void> {
		const response = await fetch('/api/v1/models');
		if (!response.ok) return;
		models = (await response.json()) as Model[];
		if (!modelId && models.length > 0) modelId = models[0].id;
	}

	async function loadHistory(): Promise<void> {
		const response = await fetch('/api/v1/generations');
		if (!response.ok) return; // degraded API: history needs the database
		history = (await response.json()) as Generation[];
	}

	$effect(() => {
		void loadModels();
		void loadHistory();
	});

	function follow(jobId: string): void {
		const source = new EventSource(`/api/v1/generations/${jobId}/events`);
		source.onmessage = (event) => {
			const data = JSON.parse(event.data) as {
				state: string;
				progress?: number;
				reason?: string;
			};
			if (data.state === 'running') {
				statusKey = 'running';
				progress = data.progress ?? null;
			} else if (data.state === 'succeeded') {
				source.close();
				busy = false;
				statusKey = 'idle';
				progress = null;
				void loadHistory();
			} else if (data.state === 'failed') {
				source.close();
				busy = false;
				statusKey = 'failed';
				progress = null;
				errorText = data.reason ?? t('app.gen.failed');
				void loadHistory();
			}
		};
		source.onerror = () => {
			// The stream keeps itself alive with comments; an error here means
			// the connection is gone. Fall back to one refresh.
			source.close();
			busy = false;
			statusKey = 'idle';
			void loadHistory();
		};
	}

	async function generate(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		errorText = '';
		const params: Record<string, unknown> = { prompt };
		if (steps !== '') params.steps = Number(steps);
		if (guidance !== '') params.guidance = Number(guidance);
		if (seed !== '') params.seed = Number(seed);
		if (size !== '512') {
			params.width = Number(size);
			params.height = Number(size);
		}
		busy = true;
		statusKey = 'queued';
		try {
			const response = await fetch('/api/v1/generations', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ model_id: modelId, params })
			});
			if (!response.ok) {
				const body = (await response.json().catch(() => null)) as { detail?: string } | null;
				throw new Error(body?.detail ?? response.statusText);
			}
			const { job_id } = (await response.json()) as { job_id: string };
			void loadHistory();
			follow(job_id);
		} catch (error) {
			busy = false;
			statusKey = 'failed';
			errorText = error instanceof Error ? error.message : String(error);
		}
	}
</script>

<div class="grid h-full min-h-0 gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
	<Card.Root class="min-h-0 overflow-y-auto">
		<Card.Header>
			<Card.Title>{t('app.gen.title')}</Card.Title>
			<Card.Description>{t('app.gen.sub')}</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if models.length === 0}
				<p class="text-muted-foreground text-sm leading-relaxed">{t('app.gen.no_models')}</p>
			{:else}
				<form class="flex flex-col gap-4" onsubmit={generate}>
					<div class="flex flex-col gap-2">
						<Label for="gen-model">{t('app.gen.model')}</Label>
						<select id="gen-model" class={fieldClass + ' h-8'} bind:value={modelId}>
							{#each models as model (model.id)}
								<option value={model.id}>{model.name}</option>
							{/each}
						</select>
					</div>
					<div class="flex flex-col gap-2">
						<Label for="gen-prompt">{t('app.gen.prompt')}</Label>
						<textarea
							id="gen-prompt"
							class={fieldClass + ' min-h-24 resize-y py-2'}
							placeholder={t('app.gen.prompt_placeholder')}
							bind:value={prompt}></textarea>
					</div>
					<div class="grid grid-cols-2 gap-3">
						<div class="flex flex-col gap-2">
							<Label for="gen-steps">{t('app.gen.steps')}</Label>
							<Input id="gen-steps" type="number" min="1" max="50" bind:value={steps} />
						</div>
						<div class="flex flex-col gap-2">
							<Label for="gen-guidance">{t('app.gen.guidance')}</Label>
							<Input id="gen-guidance" type="number" min="0" step="0.5" bind:value={guidance} />
						</div>
						<div class="flex flex-col gap-2">
							<Label for="gen-seed">{t('app.gen.seed')}</Label>
							<Input id="gen-seed" type="number" bind:value={seed} />
						</div>
						<div class="flex flex-col gap-2">
							<Label for="gen-size">{t('app.gen.size')}</Label>
							<select id="gen-size" class={fieldClass + ' h-8'} bind:value={size}>
								<option value="512">512 x 512</option>
								<option value="768">768 x 768</option>
								<option value="1024">1024 x 1024</option>
							</select>
						</div>
					</div>
					<Button type="submit" disabled={busy || prompt.trim() === ''}>
						{busy ? t('app.gen.generating') : t('app.gen.generate')}
					</Button>
					{#if statusKey === 'queued'}
						<p class="text-muted-foreground text-sm">{t('app.gen.state_queued')}</p>
					{:else if statusKey === 'running'}
						<p class="text-muted-foreground text-sm">
							{t('app.gen.state_running')}{progress !== null
								? ` ${Math.round(progress * 100)}%`
								: ''}
						</p>
					{/if}
					{#if errorText !== ''}
						<p class="text-destructive text-sm leading-relaxed">{errorText}</p>
					{/if}
				</form>
			{/if}
		</Card.Content>
	</Card.Root>

	<div class="flex min-h-0 flex-col gap-4">
		<Card.Root class="min-h-0 flex-1">
			<Card.Content class="h-full min-h-0 p-4">
				{#if latest !== null}
					<img
						src={latest.assets[0].url}
						alt={latest.params.prompt ?? t('app.gen.result')}
						class="h-full w-full rounded-lg object-contain"
					/>
				{:else}
					<div
						class="text-foreground/55 grid h-full place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
					>
						{t('app.gen.result_hint')}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
		{#if history.length > 0}
			<div class="flex shrink-0 gap-2 overflow-x-auto pb-1">
				{#each history as generation (generation.id)}
					{#if generation.assets.length > 0}
						<img
							src={generation.assets[0].url}
							alt={generation.params.prompt ?? generation.id}
							title={generation.params.prompt}
							class="border-border h-24 w-24 shrink-0 rounded-lg border object-cover"
						/>
					{:else}
						<div
							class="border-border/60 text-muted-foreground grid h-24 w-24 shrink-0 place-items-center rounded-lg border border-dashed"
						>
							<Badge variant="outline">
								{generation.state === 'failed'
									? t('app.gen.badge_failed')
									: t('app.gen.badge_working')}
							</Badge>
						</div>
					{/if}
				{/each}
			</div>
		{/if}
	</div>
</div>
