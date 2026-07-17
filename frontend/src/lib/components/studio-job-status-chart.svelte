<script lang="ts">
	import { PieChart } from 'layerchart';
	import { t } from '$lib/i18n.svelte';
	import type { JobStatusBreakdown, JobStatusSlice } from '$lib/studio-session-job-stats';
	import * as Card from '$lib/components/ui/card';
	import * as Chart from '$lib/components/ui/chart';

	let { breakdown }: { breakdown: JobStatusBreakdown } = $props();

	const visibleSlices = $derived(breakdown.slices.filter((slice) => slice.count > 0));

	const chartConfig = $derived(
		Object.fromEntries(
			breakdown.slices.map((slice) => [slice.id, { label: slice.label, color: slice.color }])
		) satisfies Chart.ChartConfig
	);
</script>

<Card.Root class="h-full overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<Card.Title class="text-base">{t('app.metrics.job_status')}</Card.Title>
		<Card.Description class="text-sm">{t('app.metrics.job_status_desc')}</Card.Description>
	</Card.Header>
	<Card.Content
		class="flex flex-col items-center gap-4 p-5 sm:flex-row sm:items-center sm:justify-between"
	>
		<div class="relative shrink-0">
			{#if breakdown.total === 0}
				<div
					class="border-border bg-muted/20 flex size-[148px] items-center justify-center rounded-full border"
				>
					<span class="text-muted-foreground text-xs">0</span>
				</div>
			{:else}
				<Chart.Container config={chartConfig} class="size-[148px]">
					<PieChart
						data={visibleSlices}
						key={(d: JobStatusSlice) => d.id}
						value={(d: JobStatusSlice) => d.count}
						label={(d: JobStatusSlice) => d.label}
						c={(d: JobStatusSlice) => d.color}
						cRange={visibleSlices.map((slice) => slice.color)}
						innerRadius={-18}
						padAngle={0.03}
						props={{ pie: { motion: 'tween' } }}
						legend={false}
					>
						{#snippet tooltip()}
							<Chart.Tooltip hideLabel />
						{/snippet}
					</PieChart>
				</Chart.Container>
				<div class="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
					<span class="text-xl font-semibold tabular-nums">{breakdown.total}</span>
					<span class="text-muted-foreground text-[10px] tracking-wide uppercase">
						{t('app.metrics.job_total')}
					</span>
				</div>
			{/if}
		</div>
		<ul class="grid w-full min-w-[12rem] gap-2 sm:max-w-xs">
			{#each breakdown.slices as slice (slice.id)}
				<li class="flex items-center justify-between gap-3 text-sm">
					<span class="flex min-w-0 items-center gap-2">
						<span class="inline-block size-2.5 shrink-0" style={`background: ${slice.color}`}
						></span>
						<span class="truncate">{slice.label}</span>
					</span>
					<span class="font-mono text-xs tabular-nums">
						{slice.count}
						{#if breakdown.total > 0}
							<span class="text-muted-foreground">
								({Math.round((slice.count / breakdown.total) * 100)}%)
							</span>
						{/if}
					</span>
				</li>
			{:else}
				<li class="text-muted-foreground text-sm">{t('app.metrics.runtime_empty')}</li>
			{/each}
		</ul>
	</Card.Content>
</Card.Root>
