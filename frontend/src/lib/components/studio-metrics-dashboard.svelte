<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import StudioMetricsPanel from '$lib/components/studio-metrics-panel.svelte';
	import { studio } from '$lib/studio.svelte';

	const title = $derived(
		studio.metricsTab === 'usage' ? t('app.metrics.tab_usage') : t('app.metrics.tab_benchmarks')
	);
</script>

<div class="flex h-full min-h-0 flex-col gap-4">
	<header class="shrink-0">
		<p class="text-muted-foreground text-sm">{t('app.metrics.title')}</p>
		<h1 class="text-2xl font-semibold tracking-tight">{title}</h1>
		<p class="text-muted-foreground mt-1 text-sm">{t('app.metrics.sub')}</p>
	</header>
	<div class="no-scrollbar min-h-0 flex-1 overflow-y-auto pb-2">
		<StudioMetricsPanel />
	</div>
</div>
