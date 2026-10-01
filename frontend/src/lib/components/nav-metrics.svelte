<script lang="ts">
	import BarChart3Icon from '@lucide/svelte/icons/bar-chart-3';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import GaugeIcon from '@lucide/svelte/icons/gauge';
	import LineChartIcon from '@lucide/svelte/icons/line-chart';
	import UserIcon from '@lucide/svelte/icons/user';
	import { t } from '$lib/i18n.svelte';
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import * as Collapsible from '$lib/components/ui/collapsible/index.js';
	import { openMetrics, studio } from '$lib/studio.svelte';
	import { account } from '$lib/account.svelte';
	import { sectionNeeds } from '$lib/account-display';

	const usageNeeds = $derived(sectionNeeds('metrics_usage', account.current?.role ?? null));
	const benchmarksNeeds = $derived(
		sectionNeeds('metrics_benchmarks', account.current?.role ?? null)
	);
	const mineNeeds = $derived(sectionNeeds('metrics_mine', account.current?.role ?? null));
	// Only a viewer is refused every tab, and the member gate is the one that
	// stops them first, so that is the badge a locked entry shows.
	const locked = $derived(usageNeeds !== null && benchmarksNeeds !== null && mineNeeds !== null);
	// A member opens on My usage: the other two tabs would be refused by the
	// URL the group entry is about to build, and land them on generate.
	const entryTab = $derived(mineNeeds === null && usageNeeds !== null ? 'mine' : studio.metricsTab);
</script>

<Collapsible.Root open class="group/collapsible">
	{#snippet child({ props })}
		<Sidebar.MenuItem {...props}>
			<Sidebar.MenuButton
				tooltipContent={t('app.shell.metrics')}
				isActive={studio.shellView === 'metrics'}
				aria-disabled={locked}
				onclick={() => {
					if (!locked) openMetrics(entryTab);
				}}
			>
				<BarChart3Icon />
				<span>{t('app.shell.metrics')}</span>
				{#if locked}
					<span class="text-sidebar-foreground/60 ml-auto truncate text-xs">
						{t('app.gen.members_only')}
					</span>
				{/if}
			</Sidebar.MenuButton>
			<Collapsible.Trigger>
				{#snippet child({ props: triggerProps })}
					<Sidebar.MenuAction {...triggerProps}>
						<ChevronRightIcon
							class="transition-transform group-data-[state=open]/collapsible:rotate-90"
						/>
						<span class="sr-only">{t('app.shell.toggle')} {t('app.shell.metrics')}</span>
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
								<button
									type="button"
									{...props}
									disabled={usageNeeds !== null}
									onclick={() => openMetrics('usage')}
								>
									<GaugeIcon />
									<span>{t('app.metrics.tab_usage')}</span>
									{#if usageNeeds !== null}
										<span class="text-sidebar-foreground/60 ml-auto truncate text-xs">
											{t('app.gen.admins_only')}
										</span>
									{/if}
								</button>
							{/snippet}
						</Sidebar.MenuSubButton>
					</Sidebar.MenuSubItem>
					<Sidebar.MenuSubItem>
						<Sidebar.MenuSubButton
							isActive={studio.shellView === 'metrics' && studio.metricsTab === 'benchmarks'}
						>
							{#snippet child({ props })}
								<button
									type="button"
									{...props}
									disabled={benchmarksNeeds !== null}
									onclick={() => openMetrics('benchmarks')}
								>
									<LineChartIcon />
									<span>{t('app.metrics.tab_benchmarks')}</span>
									{#if benchmarksNeeds !== null}
										<span class="text-sidebar-foreground/60 ml-auto truncate text-xs">
											{t('app.gen.admins_only')}
										</span>
									{/if}
								</button>
							{/snippet}
						</Sidebar.MenuSubButton>
					</Sidebar.MenuSubItem>
					<Sidebar.MenuSubItem>
						<Sidebar.MenuSubButton
							isActive={studio.shellView === 'metrics' && studio.metricsTab === 'mine'}
						>
							{#snippet child({ props })}
								<button
									type="button"
									{...props}
									disabled={mineNeeds !== null}
									onclick={() => openMetrics('mine')}
								>
									<UserIcon />
									<span>{t('app.metrics.mine_tab')}</span>
									{#if mineNeeds !== null}
										<span class="text-sidebar-foreground/60 ml-auto truncate text-xs">
											{t('app.gen.members_only')}
										</span>
									{/if}
								</button>
							{/snippet}
						</Sidebar.MenuSubButton>
					</Sidebar.MenuSubItem>
				</Sidebar.MenuSub>
			</Collapsible.Content>
		</Sidebar.MenuItem>
	{/snippet}
</Collapsible.Root>
