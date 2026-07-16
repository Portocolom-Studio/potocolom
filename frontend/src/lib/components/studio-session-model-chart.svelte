<script lang="ts">
	import { formatMs } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';
	import type { ModelRuntimeRow } from '$lib/studio-session-metrics';
	import * as Card from '$lib/components/ui/card';

	let { rows, maxGpuMs }: { rows: ModelRuntimeRow[]; maxGpuMs: number } = $props();

	const labelW = 148;
	const chartW = 360;
	const valueW = 88;
	const svgW = labelW + chartW + valueW + 24;
	const rowH = 34;
	const barH = 22;
	const gap = 2;
	const barColor = 'var(--chart-1)';

	type DisplayRow = {
		key: string;
		modelId: string;
		count: number;
		avgGpuMs: number;
	};

	const displayRows = $derived.by((): DisplayRow[] => {
		if (rows.length <= 6) {
			return rows.map((row) => ({
				key: row.modelId,
				modelId: row.modelId,
				count: row.count,
				avgGpuMs: row.avgGpuMs
			}));
		}
		const head = rows.slice(0, 5);
		const tail = rows.slice(5);
		const tailCount = tail.reduce((sum, row) => sum + row.count, 0);
		const tailTotal = tail.reduce((sum, row) => sum + row.totalGpuMs, 0);
		return [
			...head.map((row) => ({
				key: row.modelId,
				modelId: row.modelId,
				count: row.count,
				avgGpuMs: row.avgGpuMs
			})),
			{
				key: '__other__',
				modelId: t('app.metrics.other_models'),
				count: tailCount,
				avgGpuMs: tailCount > 0 ? tailTotal / tailCount : 0
			}
		];
	});

	const svgH = $derived(displayRows.length * (rowH + gap) + 8);

	let hoveredKey = $state<string | null>(null);
	let tooltipLeft = $state(0);
	let tooltipTop = $state(0);
	let chartHost: HTMLDivElement | undefined = $state();

	function barPath(x: number, y: number, width: number, height: number, radius = 4): string {
		if (width <= radius) {
			return `M ${x} ${y} h ${width} v ${height} h ${-width} Z`;
		}
		const right = x + width;
		const bottom = y + height;
		return [
			`M ${x} ${y}`,
			`L ${right - radius} ${y}`,
			`Q ${right} ${y} ${right} ${y + radius}`,
			`L ${right} ${bottom - radius}`,
			`Q ${right} ${bottom} ${right - radius} ${bottom}`,
			`L ${x} ${bottom}`,
			`L ${x} ${y}`,
			'Z'
		].join(' ');
	}

	function showBarTooltip(row: DisplayRow, event: PointerEvent) {
		hoveredKey = row.key;
		if (!chartHost) return;
		const rect = chartHost.getBoundingClientRect();
		tooltipLeft = event.clientX - rect.left + 12;
		tooltipTop = event.clientY - rect.top - 24;
	}

	function hideBarTooltip() {
		hoveredKey = null;
	}

	const hoveredRow = $derived(
		hoveredKey == null ? null : (displayRows.find((row) => row.key === hoveredKey) ?? null)
	);
</script>

<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<Card.Title class="text-base">{t('app.metrics.by_model')}</Card.Title>
		<Card.Description class="text-sm">{t('app.metrics.by_model_desc')}</Card.Description>
	</Card.Header>
	<Card.Content class="overflow-x-auto p-4">
		<div class="relative" bind:this={chartHost}>
			<svg
				viewBox="0 0 {svgW} {svgH}"
				class="h-auto w-full min-w-[32rem]"
				role="img"
				aria-label={t('app.metrics.by_model')}
			>
				{#each displayRows as row, index (row.key)}
					{@const y = index * (rowH + gap) + 4}
					{@const barW = Math.max(4, (row.avgGpuMs / maxGpuMs) * chartW)}
					<text x="0" y={y + rowH / 2 + 4} class="fill-muted-foreground font-mono text-[11px]">
						{row.modelId.length > 18 ? `${row.modelId.slice(0, 17)}…` : row.modelId}
					</text>
					<rect
						x={labelW}
						y={y + (rowH - barH) / 2}
						width={chartW}
						height={barH}
						class="fill-muted/40"
					/>
					<path
						d={barPath(labelW, y + (rowH - barH) / 2, barW, barH)}
						fill={barColor}
						opacity={hoveredKey == null || hoveredKey === row.key ? 0.92 : 0.78}
						class="cursor-pointer"
						role="button"
						tabindex="-1"
						aria-label="{row.modelId}: {formatMs(row.avgGpuMs)}"
						onpointerenter={(event) => showBarTooltip(row, event)}
						onpointermove={(event) => showBarTooltip(row, event)}
						onpointerleave={hideBarTooltip}
						onpointerdown={(event) => showBarTooltip(row, event)}
					/>
					<text
						x={labelW + barW + 8}
						y={y + rowH / 2 + 4}
						class="fill-foreground font-mono text-[11px] tabular-nums"
					>
						{formatMs(row.avgGpuMs)}
					</text>
					<text
						x={labelW + chartW + 12}
						y={y + rowH / 2 + 16}
						class="fill-muted-foreground text-[10px] tabular-nums"
					>
						{row.count}
						{t('app.metrics.samples')}
					</text>
				{/each}
			</svg>

			{#if hoveredRow}
				<div
					class="bg-popover text-popover-foreground border-border pointer-events-none absolute z-10 rounded-md border px-2 py-1 text-xs shadow-sm"
					style={`left: ${tooltipLeft}px; top: ${tooltipTop}px;`}
					role="tooltip"
				>
					<div class="max-w-[12rem] truncate">{hoveredRow.modelId}</div>
					<div class="text-foreground mt-0.5 font-mono tabular-nums">
						{formatMs(hoveredRow.avgGpuMs)}
						<span class="text-muted-foreground">
							· {hoveredRow.count}
							{t('app.metrics.samples')}
						</span>
					</div>
				</div>
			{/if}
		</div>
	</Card.Content>
</Card.Root>
