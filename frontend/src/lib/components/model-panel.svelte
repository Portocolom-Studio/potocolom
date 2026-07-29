<script lang="ts">
	import { formatMs } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import { addModel, modelIsRemoved, removeModel, studio, type Model } from '$lib/studio.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';

	const allRemoved = $derived(
		studio.models.length > 0 && studio.models.every((model) => modelIsRemoved(model.id))
	);

	function capabilityLabel(capability: string): string {
		const labels: Record<string, string> = {
			text_to_image: t('app.models.cap_text_to_image'),
			image_to_image: t('app.models.cap_image_to_image'),
			upscale: t('app.models.cap_upscale'),
			realtime: t('app.models.cap_realtime')
		};
		return labels[capability] ?? capability.replaceAll('_', ' ');
	}

	function factorEstimates(model: Model): [string, number][] {
		return Object.entries(model.estimated_gpu_ms_by_factor ?? {}).sort(
			([left], [right]) => Number(left) - Number(right)
		);
	}
</script>

<div class="no-scrollbar h-full overflow-y-auto">
	<div class="mx-auto flex w-full max-w-6xl flex-col gap-4 pb-4">
		<div>
			<h1 class="text-xl font-semibold">{t('app.models.title')}</h1>
			<p class="text-muted-foreground mt-1 max-w-3xl text-sm leading-relaxed">
				{t('app.models.sub')}
			</p>
		</div>

		{#if allRemoved}
			<div class="border-border bg-muted/30 rounded-lg border px-4 py-3 text-sm">
				<p class="font-medium">{t('app.models.all_removed_title')}</p>
				<p class="text-muted-foreground mt-1">{t('app.models.all_removed_sub')}</p>
			</div>
		{/if}

		{#if studio.models.length === 0}
			<Card.Root>
				<Card.Content class="text-muted-foreground p-6 text-sm">
					{t('app.models.empty')}
				</Card.Content>
			</Card.Root>
		{:else}
			<div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
				{#each studio.models as model (model.id)}
					{@const removed = modelIsRemoved(model.id)}
					{@const estimates = factorEstimates(model)}
					<Card.Root class={removed ? 'border-dashed opacity-70' : ''}>
						<Card.Header class="gap-3">
							<div class="flex min-w-0 items-start justify-between gap-3">
								<div class="min-w-0">
									<Card.Title class="truncate text-base">{model.name}</Card.Title>
									<Card.Description class="mt-1 truncate font-mono text-xs">
										{model.id}
									</Card.Description>
								</div>
								<Badge variant={removed ? 'outline' : 'secondary'}>
									{removed ? t('app.models.removed') : t('app.models.in_pickers')}
								</Badge>
							</div>
							<div class="flex flex-wrap gap-1.5">
								{#each model.capabilities as capability (capability)}
									<Badge variant="outline">{capabilityLabel(capability)}</Badge>
								{/each}
							</div>
						</Card.Header>
						<Card.Content class="flex flex-col gap-4">
							<div class="grid grid-cols-2 gap-3 text-sm">
								<div>
									<p class="text-muted-foreground text-xs">{t('app.models.vram')}</p>
									<p class="mt-1 font-medium">{model.min_vram_gb} GB</p>
								</div>
								<div>
									<p class="text-muted-foreground text-xs">{t('app.models.gpu_estimate')}</p>
									<p class="mt-1 font-medium">
										{model.estimated_gpu_ms_default == null
											? t('app.models.not_measured')
											: formatMs(model.estimated_gpu_ms_default)}
									</p>
								</div>
							</div>
							{#if estimates.length > 0}
								<div>
									<p class="text-muted-foreground mb-2 text-xs">
										{t('app.models.factor_estimates')}
									</p>
									<div class="flex flex-wrap gap-2">
										{#each estimates as [factor, estimate] (factor)}
											<span class="bg-muted rounded-md px-2 py-1 text-xs tabular-nums">
												x{factor}: {formatMs(estimate)}
											</span>
										{/each}
									</div>
								</div>
							{/if}
						</Card.Content>
						<Card.Footer>
							<Button
								variant={removed ? 'default' : 'outline'}
								size="sm"
								onclick={() => (removed ? addModel(model.id) : removeModel(model.id))}
							>
								{removed ? t('app.models.add') : t('app.models.remove')}
							</Button>
						</Card.Footer>
					</Card.Root>
				{/each}
			</div>
		{/if}
	</div>
</div>
