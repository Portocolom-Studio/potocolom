<script lang="ts">
	import { sectionNeeds } from '$lib/account-display';
	import { account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';
	import StudioHeaderTabs from '$lib/components/studio-header-tabs.svelte';
	import StudioMetricsPanel from '$lib/components/studio-metrics-panel.svelte';
	import { openMetrics, studio } from '$lib/studio.svelte';
	import { useStudioHeader } from '$lib/studio-chrome.svelte';
	import type { MetricsTab } from '$lib/studio-view';

	const role = $derived(account.current?.role ?? null);

	function lockReason(needs: 'user' | 'admin' | null): string | null {
		if (needs === null) return null;
		return needs === 'user' ? t('app.gen.members_only') : t('app.gen.admins_only');
	}

	const tabs = $derived([
		{
			value: 'usage',
			label: t('app.metrics.tab_usage'),
			locked: lockReason(sectionNeeds('metrics_usage', role))
		},
		{
			value: 'benchmarks',
			label: t('app.metrics.tab_benchmarks'),
			locked: lockReason(sectionNeeds('metrics_benchmarks', role))
		},
		{
			value: 'mine',
			label: t('app.metrics.mine_tab'),
			locked: lockReason(sectionNeeds('metrics_mine', role))
		}
	]);

	const title = $derived(tabs.find((tab) => tab.value === studio.metricsTab)?.label ?? '');

	useStudioHeader(() => ({ center: headerTabs }));
</script>

{#snippet headerTabs()}
	<StudioHeaderTabs
		label={t('app.metrics.title')}
		{tabs}
		current={studio.metricsTab}
		onSelect={(value) => openMetrics(value as MetricsTab)}
	/>
{/snippet}

<div class="no-scrollbar h-full min-h-0 overflow-y-auto">
	<div class="flex flex-col gap-3 px-4 py-5 md:px-6">
		<header class="md:sr-only">
			<h1 class="text-xl font-semibold tracking-[-0.02em]">{title}</h1>
			<p class="text-muted-foreground mt-1 text-[12.5px]">{t('app.metrics.sub')}</p>
		</header>
		<nav class="flex gap-1 md:hidden" aria-label={t('app.metrics.title')}>
			{#each tabs as tab (tab.value)}
				<button
					type="button"
					class={'rounded-md px-2.5 py-1 text-xs font-medium aria-disabled:opacity-50 ' +
						(studio.metricsTab === tab.value
							? 'bg-hover text-foreground'
							: 'text-muted-foreground')}
					aria-disabled={tab.locked !== null}
					title={tab.locked ?? undefined}
					aria-current={studio.metricsTab === tab.value ? 'page' : undefined}
					onclick={() => {
						if (tab.locked === null) openMetrics(tab.value as MetricsTab);
					}}
					>{tab.label}{#if tab.locked}<span class="sr-only"> ({tab.locked})</span>{/if}</button
				>
			{/each}
		</nav>
		<StudioMetricsPanel />
	</div>
</div>
