<script lang="ts">
	import BarChart3Icon from '@lucide/svelte/icons/bar-chart-3';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import GaugeIcon from '@lucide/svelte/icons/gauge';
	import LineChartIcon from '@lucide/svelte/icons/line-chart';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';
	import { openMetrics, studio } from '$lib/studio.svelte';
</script>

<Collapsible.Root open class="group/collapsible">
	{#snippet child({ props })}
		<Sidebar.MenuItem {...props}>
			<Sidebar.MenuButton
				tooltipContent={t('app.shell.metrics')}
				isActive={studio.shellView === 'metrics'}
				onclick={() => openMetrics(studio.metricsTab)}
			>
				<BarChart3Icon />
				<span>{t('app.shell.metrics')}</span>
			</Sidebar.MenuButton>
			<Collapsible.Trigger>
				{#snippet child({ props: triggerProps })}
					<Sidebar.MenuAction {...triggerProps}>
						<ChevronRightIcon
							class="transition-transform group-data-[state=open]/collapsible:rotate-90"
						/>
					</Sidebar.MenuAction>
				{/snippet}
			</Collapsible.Trigger>
			<Collapsible.Content>
				<Sidebar.MenuSub>
					<Sidebar.MenuSubItem>
						<Sidebar.MenuSubButton
							isActive={studio.shellView === 'metrics' && studio.metricsTab === 'usage'}
						>
							{#snippet child({ props })}
								<button type="button" {...props} onclick={() => openMetrics('usage')}>
									<GaugeIcon />
									<span>{t('app.metrics.tab_usage')}</span>
								</button>
							{/snippet}
						</Sidebar.MenuSubButton>
					</Sidebar.MenuSubItem>
					<Sidebar.MenuSubItem>
						<Sidebar.MenuSubButton
							isActive={studio.shellView === 'metrics' && studio.metricsTab === 'benchmarks'}
						>
							{#snippet child({ props })}
								<button type="button" {...props} onclick={() => openMetrics('benchmarks')}>
									<LineChartIcon />
									<span>{t('app.metrics.tab_benchmarks')}</span>
								</button>
							{/snippet}
						</Sidebar.MenuSubButton>
					</Sidebar.MenuSubItem>
				</Sidebar.MenuSub>
			</Collapsible.Content>
		</Sidebar.MenuItem>
	{/snippet}
</Collapsible.Root>
