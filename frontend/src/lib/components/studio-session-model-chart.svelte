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
	const gap = 10;
	const svgH = $derived(rows.length * (rowH + gap) + 8);

	const palette = [
		'var(--chart-1)',
		'var(--chart-2)',
		'var(--chart-3)',
		'var(--chart-4)',
		'var(--chart-5)'
	];
</script>

<Card.Root class="overflow-hidden p-0 [--card-spacing:0]">
	<Card.Header class="border-border border-b px-5 py-4">
		<Card.Title class="text-base">{t('app.metrics.by_model')}</Card.Title>
		<Card.Description class="text-sm">{t('app.metrics.by_model_desc')}</Card.Description>
	</Card.Header>
	<Card.Content class="overflow-x-auto p-4">
		<svg
			viewBox="0 0 {svgW} {svgH}"
			class="h-auto w-full min-w-[32rem]"
			role="img"
			aria-label={t('app.metrics.by_model')}
		>
			{#each rows as row, index (row.modelId)}
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
					rx="6"
					class="fill-muted/40"
				/>
				<rect
					x={labelW}
					y={y + (rowH - barH) / 2}
					width={barW}
					height={barH}
					rx="6"
					fill={palette[index % palette.length]}
					opacity="0.92"
				/>
				<text
					x={labelW + chartW + 12}
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
	</Card.Content>
</Card.Root>
