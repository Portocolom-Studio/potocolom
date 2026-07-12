<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import * as Card from '$lib/components/ui/card';
	import { Input } from '$lib/components/ui/input';
	import { Label } from '$lib/components/ui/label';
	import { loadHistory, loadModels, pollWhileWorking, studio } from '$lib/studio.svelte';

	// Matches the Input component's field styling for the native controls it
	// does not vendor (select, textarea).
	const fieldClass =
		'dark:bg-input/30 border-input focus-visible:border-ring focus-visible:ring-ring/50 ' +
		'placeholder:text-muted-foreground w-full min-w-0 rounded-lg border bg-transparent ' +
		'px-2.5 py-1 text-base transition-colors outline-none focus-visible:ring-3 md:text-sm ' +
		'disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50';

	let steps = $state('');
	let guidance = $state('');
	let seed = $state('');
	let size = $state('512');
	let count = $state('1');
	let errorText = $state('');

	// The viewer shows the clicked generation, or the newest finished one.
	const shown = $derived(
		studio.history.find((g) => g.id === studio.selectedId && g.assets.length > 0) ??
			studio.history.find((g) => g.assets.length > 0) ??
			null
	);
	// Jobs queue server side; submitting never blocks the form (docs/blueprint.md,
	// the generation request path returns a job id immediately).
	const working = $derived(
		studio.history.filter((g) => g.state === 'queued' || g.state === 'running').length
	);

	// The manifest schema decides which resolutions a model supports
	// (docs/architecture.md, model manifests); no enum means unconstrained.
	const selectedModel = $derived(studio.models.find((m) => m.id === studio.modelId));
	const sizeOptions = $derived(
		selectedModel?.parameters.properties?.width?.enum ?? [512, 768, 1024]
	);

	$effect(() => {
		if (!sizeOptions.includes(Number(size))) size = String(sizeOptions[0]);
	});

	$effect(() => {
		void loadModels();
		void loadHistory().then(pollWhileWorking);
	});

	async function generate(event: SubmitEvent): Promise<void> {
		event.preventDefault();
		errorText = '';
		studio.selectedId = null; // let fresh results take the viewer back
		const jobs = Math.min(Math.max(Number(count) || 1, 1), 8);
		for (let index = 0; index < jobs; index += 1) {
			const params: Record<string, unknown> = { prompt: studio.prompt };
			if (steps !== '') params.steps = Number(steps);
			if (guidance !== '') params.guidance = Number(guidance);
			// A fixed seed still varies across a batch, or every image would be identical.
			if (seed !== '') params.seed = Number(seed) + index;
			params.width = Number(size);
			params.height = Number(size);
			const response = await fetch('/api/v1/generations', {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ model_id: studio.modelId, params })
			});
			if (!response.ok) {
				const body = (await response.json().catch(() => null)) as { detail?: string } | null;
				errorText = body?.detail ?? response.statusText;
				break;
			}
		}
		await loadHistory();
		void pollWhileWorking();
	}
</script>

<div class="grid h-full min-h-0 gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
	<Card.Root class="min-h-0 overflow-y-auto">
		<Card.Header>
			<Card.Title>{t('app.gen.title')}</Card.Title>
			<Card.Description>{t('app.gen.sub')}</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if studio.models.length === 0}
				<p class="text-muted-foreground text-sm leading-relaxed">{t('app.gen.no_models')}</p>
			{:else}
				<form class="flex flex-col gap-4" onsubmit={generate}>
					<div class="flex flex-col gap-2">
						<Label for="gen-model">{t('app.gen.model')}</Label>
						<select id="gen-model" class={fieldClass + ' h-8'} bind:value={studio.modelId}>
							{#each studio.models as model (model.id)}
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
							bind:value={studio.prompt}></textarea>
					</div>
					<div class="grid grid-cols-2 gap-3">
						<div class="flex flex-col gap-2">
							<Label for="gen-count">{t('app.gen.count')}</Label>
							<Input id="gen-count" type="number" min="1" max="8" bind:value={count} />
						</div>
						<div class="flex flex-col gap-2">
							<Label for="gen-size">{t('app.gen.size')}</Label>
							<select
								id="gen-size"
								class={fieldClass + ' h-8'}
								bind:value={size}
								disabled={sizeOptions.length === 1}
							>
								{#each sizeOptions as option (option)}
									<option value={String(option)}>{option} x {option}</option>
								{/each}
							</select>
						</div>
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
					</div>
					<Button type="submit" disabled={studio.prompt.trim() === ''}>
						{t('app.gen.generate')}
					</Button>
					{#if working > 0}
						<p class="text-muted-foreground text-sm">
							{working}
							{t('app.gen.working_suffix')}
						</p>
					{/if}
					{#if errorText !== ''}
						<p class="text-destructive text-sm leading-relaxed">{errorText}</p>
					{/if}
				</form>
			{/if}
		</Card.Content>
	</Card.Root>

	<!-- min-w-0: the thumbnail strip's intrinsic width must not widen the grid track -->
	<div class="flex min-h-0 min-w-0 flex-col gap-4">
		<Card.Root class="min-h-0 flex-1">
			<Card.Content class="flex h-full min-h-0 flex-col gap-2 p-4">
				{#if shown !== null}
					<a
						href={shown.assets[0].url}
						target="_blank"
						rel="noopener"
						class="block min-h-0 flex-1"
						title={t('app.gen.open_full')}
					>
						<img
							src={shown.assets[0].url}
							alt={shown.params.prompt ?? t('app.gen.result')}
							class="h-full w-full rounded-lg object-contain"
						/>
					</a>
					<p class="text-muted-foreground shrink-0 truncate text-center text-xs">
						{shown.params.prompt}
					</p>
				{:else}
					<div
						class="text-foreground/55 grid h-full place-items-center px-6 text-center text-xs tracking-[0.14em] uppercase"
					>
						{t('app.gen.result_hint')}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
		{#if studio.history.length > 0}
			<div class="flex shrink-0 gap-2 overflow-x-auto pb-1">
				{#each studio.history as generation (generation.id)}
					{#if generation.assets.length > 0}
						<button
							type="button"
							class="shrink-0"
							title={generation.params.prompt}
							onclick={() => (studio.selectedId = generation.id)}
						>
							<img
								src={generation.assets[0].url}
								alt={generation.params.prompt ?? generation.id}
								class={'h-24 w-24 rounded-lg border object-cover ' +
									(shown?.id === generation.id ? 'border-primary' : 'border-border')}
							/>
						</button>
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
