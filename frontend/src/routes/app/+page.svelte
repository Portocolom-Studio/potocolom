<script lang="ts">
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import { resolve } from '$app/paths';
	import * as Sidebar from '$lib/components/ui/sidebar';
	import { Separator } from '$lib/components/ui/separator';
	import BrushIcon from '@lucide/svelte/icons/brush';
	import SparklesIcon from '@lucide/svelte/icons/sparkles';
	import SquarePenIcon from '@lucide/svelte/icons/square-pen';
	import WandSparklesIcon from '@lucide/svelte/icons/wand-sparkles';

	const tabs = [
		{ id: 'draw', icon: BrushIcon },
		{ id: 'generate', icon: SparklesIcon },
		{ id: 'edit', icon: SquarePenIcon },
		{ id: 'enhance', icon: WandSparklesIcon }
	] as const;
	type Tab = (typeof tabs)[number]['id'];

	let active = $state<Tab>('draw');
</script>

<svelte:head>
	<title>potocolom - {t('app.title')}</title>
</svelte:head>

<Sidebar.Provider>
	<Sidebar.Root collapsible="icon">
		<Sidebar.Header>
			<a
				class="flex h-8 items-center px-2 font-bold tracking-tight"
				href={resolve('/')}
				title={t('app.back')}
			>
				<span class="group-data-[collapsible=icon]:hidden">
					potocolom<span class="text-primary">_</span>
				</span>
				<span class="text-primary hidden group-data-[collapsible=icon]:inline">p_</span>
			</a>
		</Sidebar.Header>
		<Sidebar.Content>
			<Sidebar.Group>
				<Sidebar.GroupLabel>{t('app.title')}</Sidebar.GroupLabel>
				<Sidebar.GroupContent>
					<Sidebar.Menu>
						{#each tabs as tab (tab.id)}
							<Sidebar.MenuItem>
								<Sidebar.MenuButton
									isActive={active === tab.id}
									tooltipContent={t(`app.tab_${tab.id}`)}
									onclick={() => (active = tab.id)}
								>
									<tab.icon />
									<span>{t(`app.tab_${tab.id}`)}</span>
								</Sidebar.MenuButton>
							</Sidebar.MenuItem>
						{/each}
					</Sidebar.Menu>
				</Sidebar.GroupContent>
			</Sidebar.Group>
		</Sidebar.Content>
	</Sidebar.Root>

	<Sidebar.Inset>
		<header class="flex h-14 items-center gap-3 border-b px-4">
			<Sidebar.Trigger />
			<Separator orientation="vertical" class="h-5" />
			<h1 class="text-sm font-medium">{t(`app.tab_${active}`)}</h1>
			<div class="ml-auto">
				<LanguageToggle />
			</div>
		</header>

		<main class="flex flex-1 flex-col gap-4 p-4">
			{#if active === 'draw'}
				<div class="grid min-h-[60vh] flex-1 grid-cols-1 gap-4 md:grid-cols-2">
					<div class="bg-card/50 grid place-items-center rounded-xl border border-dashed">
						<span class="text-muted-foreground text-xs tracking-[0.14em] uppercase">
							{t('app.canvas_hint')}
						</span>
					</div>
					<div class="bg-card/50 grid place-items-center rounded-xl border border-dashed">
						<span class="text-muted-foreground text-xs tracking-[0.14em] uppercase">
							{t('app.result_hint')}
						</span>
					</div>
				</div>
				<p class="text-muted-foreground text-sm">{t('app.draw_placeholder')}</p>
			{:else}
				<div class="bg-card/50 grid min-h-[60vh] flex-1 rounded-xl border border-dashed"></div>
				<p class="text-muted-foreground text-sm">{t(`app.${active}_placeholder`)}</p>
			{/if}
		</main>
	</Sidebar.Inset>
</Sidebar.Provider>
