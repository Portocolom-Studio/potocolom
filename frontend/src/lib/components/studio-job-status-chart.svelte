<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import type { JobStatusBreakdown, JobStatusSlice } from '$lib/studio-session-job-stats';
	import * as Card from '$lib/components/ui/card';

	let { breakdown }: { breakdown: JobStatusBreakdown } = $props();

	const size = 148;
	const cx = size / 2;
	const cy = size / 2;
	const outerR = 58;
	const innerR = 38;
	const gapRad = 0.04;

	let hoveredId = $state<JobStatusSlice['id'] | null>(null);
	let tooltipLeft = $state(0);
	let tooltipTop = $state(0);
	let chartHost: HTMLDivElement | undefined = $state();

	const visibleSlices = $derived(breakdown.slices.filter((slice) => slice.count > 0));

	const arcs = $derived.by(() => {
		if (breakdown.total === 0 || visibleSlices.length === 0) return [];
		let angle = -Math.PI / 2;
		return visibleSlices.map((slice) => {
			const sweep = (slice.count / breakdown.total) * Math.PI * 2;
			const paddedSweep = Math.max(0, sweep - gapRad);
			const start = angle + gapRad / 2;
			const end = start + paddedSweep;
			angle += sweep;
			return { slice, start, end };
		});
	});

	function ringPath(start: number, end: number): string {
		const large = end - start > Math.PI ? 1 : 0;
		const x1o = cx + outerR * Math.cos(start);
		const y1o = cy + outerR * Math.sin(start);
		const x2o = cx + outerR * Math.cos(end);
		const y2o = cy + outerR * Math.sin(end);
		const x1i = cx + innerR * Math.cos(end);
		const y1i = cy + innerR * Math.sin(end);
		const x2i = cx + innerR * Math.cos(start);
		const y2i = cy + innerR * Math.sin(start);
		return [
			`M ${x1o} ${y1o}`,
			`A ${outerR} ${outerR} 0 ${large} 1 ${x2o} ${y2o}`,
			`L ${x1i} ${y1i}`,
			`A ${innerR} ${innerR} 0 ${large} 0 ${x2i} ${y2i}`,
			'Z'
		].join(' ');
	}

	function showTooltip(slice: JobStatusSlice, event: PointerEvent) {
		hoveredId = slice.id;
		if (!chartHost) return;
		const rect = chartHost.getBoundingClientRect();
		tooltipLeft = event.clientX - rect.left + 12;
		tooltipTop = event.clientY - rect.top - 28;
	}

	function hideTooltip() {
		hoveredId = null;
	}

	const hoveredSlice = $derived(
		hoveredId == null ? null : (breakdown.slices.find((slice) => slice.id === hoveredId) ?? null)
	);
</script>

<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<Card.Title class="text-base">{t('app.metrics.job_status')}</Card.Title>
		<Card.Description class="text-sm">{t('app.metrics.job_status_desc')}</Card.Description>
	</Card.Header>
	<Card.Content
		class="flex flex-col items-center gap-4 p-5 sm:flex-row sm:items-center sm:justify-between"
	>
		<div class="relative shrink-0" bind:this={chartHost}>
			<svg
				width={size}
				height={size}
				viewBox="0 0 {size} {size}"
				role="img"
				aria-label={t('app.metrics.job_status')}
			>
				{#if breakdown.total === 0}
					<circle {cx} {cy} r={outerR} class="fill-muted/30 stroke-border" stroke-width="1" />
					<circle {cx} {cy} r={innerR} class="fill-background" />
				{:else}
					{#each arcs as arc (arc.slice.id)}
						<path
							d={ringPath(arc.start, arc.end)}
							fill={arc.slice.color}
							opacity={hoveredId == null || hoveredId === arc.slice.id ? 1 : 0.85}
							class="cursor-pointer"
							role="button"
							tabindex="-1"
							aria-label="{arc.slice.label}: {arc.slice.count}"
							onpointerenter={(event) => showTooltip(arc.slice, event)}
							onpointermove={(event) => showTooltip(arc.slice, event)}
							onpointerleave={hideTooltip}
							onpointerdown={(event) => showTooltip(arc.slice, event)}
						/>
					{/each}
					<circle {cx} {cy} r={innerR} class="fill-background" />
				{/if}
				<text
					x={cx}
					y={cy - 4}
					text-anchor="middle"
					class="fill-foreground text-xl font-semibold tabular-nums"
				>
					{breakdown.total}
				</text>
				<text
					x={cx}
					y={cy + 14}
					text-anchor="middle"
					class="fill-muted-foreground text-[10px] uppercase tracking-wide"
				>
					{t('app.metrics.job_total')}
				</text>
			</svg>

			{#if hoveredSlice && breakdown.total > 0}
				{@const pct = Math.round((hoveredSlice.count / breakdown.total) * 100)}
				<div
					class="bg-popover text-popover-foreground border-border pointer-events-none absolute z-10 rounded-md border px-2 py-1 text-xs shadow-sm"
					style={`left: ${tooltipLeft}px; top: ${tooltipTop}px;`}
					role="tooltip"
				>
					<div class="flex items-center gap-1.5">
						<span
							class="inline-block size-2 shrink-0 rounded-sm"
							style={`background: ${hoveredSlice.color}`}
							aria-hidden="true"
						></span>
						<span>{hoveredSlice.label}</span>
					</div>
					<div class="text-foreground mt-0.5 font-mono tabular-nums">
						{hoveredSlice.count}
						<span class="text-muted-foreground">({pct}%)</span>
					</div>
				</div>
			{/if}
		</div>
		<ul class="grid w-full min-w-[12rem] gap-2 sm:max-w-xs">
			{#each breakdown.slices as slice (slice.id)}
				<li class="flex items-center justify-between gap-3 text-sm">
					<span class="flex min-w-0 items-center gap-2">
						<span
							class="inline-block size-2.5 shrink-0 rounded-sm"
							style={`background: ${slice.color}`}
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
