<script lang="ts">
	import { tick } from 'svelte';
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import ChevronLeftIcon from '@lucide/svelte/icons/chevron-left';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import {
		loadOlderHistory,
		resetHistoryToRecent,
		studio,
		type Generation
	} from '$lib/studio.svelte';

	let stripEl = $state<HTMLDivElement | null>(null);
	let loadingOlder = $state(false);
	let loadError = $state('');

	const shownId = $derived(
		studio.selectedId ?? studio.history.find((g) => g.assets.length > 0)?.id ?? null
	);

	async function loadOlder(): Promise<void> {
		if (!stripEl || loadingOlder) return;
		loadingOlder = true;
		loadError = '';
		const atEnd = stripEl.scrollLeft + stripEl.clientWidth >= stripEl.scrollWidth - 8;
		const prevWidth = stripEl.scrollWidth;
		const loaded = await loadOlderHistory();
		await tick();
		if (loaded && stripEl) {
			if (atEnd) {
				stripEl.scrollLeft = stripEl.scrollWidth - stripEl.clientWidth;
			} else {
				stripEl.scrollLeft += stripEl.scrollWidth - prevWidth;
			}
		} else if (!loaded) {
			loadError = t('app.gen.load_older_empty');
		}
		loadingOlder = false;
	}

	async function backToRecent(): Promise<void> {
		resetHistoryToRecent();
		await tick();
		stripEl?.scrollTo({ left: 0, behavior: 'smooth' });
	}

	function select(generation: Generation): void {
		studio.selectedId = generation.id;
	}
</script>

{#if studio.history.length > 0}
	<div class="flex shrink-0 gap-2 overflow-x-auto pb-1" bind:this={stripEl}>
		{#if studio.historyExtended}
			<button
				type="button"
				class="border-border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground flex h-24 w-14 shrink-0 flex-col items-center justify-center gap-1 rounded-lg border text-[0.65rem] leading-tight transition-colors"
				title={t('app.gen.back_to_recent')}
				onclick={backToRecent}
			>
				<ChevronLeftIcon class="size-4" />
				<span class="max-w-[2.5rem] text-center">{t('app.gen.back_to_recent')}</span>
			</button>
		{/if}

		{#each studio.history.filter((g) => g.state !== 'failed') as generation (generation.id)}
			{#if generation.assets.length > 0}
				<button
					type="button"
					class="shrink-0"
					title={generation.params.prompt}
					onclick={() => select(generation)}
				>
					<img
						src={generation.assets[0].url}
						alt={generation.params.prompt ?? generation.id}
						class={'h-24 w-24 rounded-lg border object-cover ' +
							(shownId === generation.id ? 'border-primary' : 'border-border')}
					/>
				</button>
			{:else if generation.state === 'queued' || generation.state === 'running'}
				<div
					class="border-border/60 text-muted-foreground relative grid h-24 w-24 shrink-0 place-items-center rounded-lg border border-dashed"
				>
					<Badge variant="outline">
						{t('app.gen.badge_working')}
					</Badge>
					{#if generation.state === 'running' && generation.progress !== null}
						<div class="bg-border absolute inset-x-3 bottom-2 h-1 rounded-full">
							<div
								class="bg-primary h-1 rounded-full transition-[width]"
								style={`width: ${Math.round(generation.progress * 100)}%`}
							></div>
						</div>
					{/if}
				</div>
			{/if}
		{/each}

		{#if studio.historyHasMore}
			<button
				type="button"
				class="border-border bg-muted/40 text-muted-foreground hover:bg-muted hover:text-foreground flex h-24 w-14 shrink-0 flex-col items-center justify-center gap-1 rounded-lg border text-[0.65rem] leading-tight transition-colors disabled:opacity-50"
				title={t('app.gen.load_older')}
				disabled={loadingOlder}
				onclick={loadOlder}
			>
				<ChevronRightIcon class="size-4" />
				<span class="max-w-[2.5rem] text-center">{t('app.gen.load_older')}</span>
			</button>
		{/if}
	</div>
	{#if loadError !== ''}
		<p class="text-muted-foreground text-xs">{loadError}</p>
	{/if}
{/if}
