<script lang="ts">
	import { type CategoryLineSeries } from '$lib/benchmark';
	import { t } from '$lib/i18n.svelte';

	let {
		categories,
		series
	}: {
		categories: string[];
		series: CategoryLineSeries[];
	} = $props();

	const size = 400;
	const cx = size / 2;
	const cy = size / 2;
	const radius = size * 0.34;
	const labelRadius = size * 0.44;

	const categoryBounds = $derived(
		categories.map((category) => {
			const values = series
				.map(
					(row) => row.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0
				)
				.filter((ms) => ms > 0);
			const max = Math.max(...values, 1);
			const min = Math.min(...values, max);
			return { max, min };
		})
	);

	const gridLevels = [0.25, 0.5, 0.75, 1];

	function axisAngle(index: number): number {
		return -Math.PI / 2 + ((2 * Math.PI) / categories.length) * index;
	}

	function polar(index: number, scale: number): { x: number; y: number } {
		const angle = axisAngle(index);
		return {
			x: cx + Math.cos(angle) * radius * scale,
			y: cy + Math.sin(angle) * radius * scale
		};
	}

	function ringPath(scale: number): string {
		return categories
			.map((_, index) => {
				const point = polar(index, scale);
				return `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`;
			})
			.join(' ') + ' Z';
	}

	function score(ms: number, categoryIndex: number): number {
		const { max, min } = categoryBounds[categoryIndex];
		if (max <= min) return 1;
		return (max - ms) / (max - min);
	}

	function seriesPath(row: CategoryLineSeries): string {
		return (
			categories
				.map((category, index) => {
					const ms = row.points.find((point) => point.category === category)?.avg_gpu_ms ?? 0;
					const point = polar(index, score(ms, index));
					return `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`;
				})
				.join(' ') + ' Z'
		);
	}

	function labelPoint(index: number): { x: number; y: number; anchor: string } {
		const angle = axisAngle(index);
		const x = cx + Math.cos(angle) * labelRadius;
		const y = cy + Math.sin(angle) * labelRadius;
		const anchor =
			Math.abs(Math.cos(angle)) < 0.2 ? 'middle' : Math.cos(angle) > 0 ? 'start' : 'end';
		return { x, y, anchor };
	}
</script>

<svg
	viewBox="0 0 {size} {size}"
	class="mx-auto w-full max-w-md"
	role="img"
	aria-label={t('bench.chart_category')}
>
	{#each gridLevels as level (level)}
		<path
			d={ringPath(level)}
			fill="none"
			class="stroke-border/70"
			stroke-width="1"
			stroke-dasharray={level < 1 ? '4 4' : '0'}
		/>
	{/each}

	{#each categories as _, index (index)}
		{@const outer = polar(index, 1)}
		<line x1={cx} y1={cy} x2={outer.x} y2={outer.y} class="stroke-border/50" stroke-width="1" />
	{/each}

	{#each series as row, index (row.model_id)}
		<path
			d={seriesPath(row)}
			fill={row.color}
			fill-opacity="0.14"
			stroke={row.color}
			stroke-width="2"
			stroke-linejoin="round"
		/>
	{/each}

	{#each categories as category, index (category)}
		{@const label = labelPoint(index)}
		<text
			x={label.x}
			y={label.y}
			text-anchor={label.anchor}
			dominant-baseline="middle"
			class="fill-muted-foreground text-[10px] capitalize"
		>
			{category}
		</text>
	{/each}
</svg>

<div class="mt-4 flex flex-wrap justify-center gap-x-4 gap-y-2">
	{#each series as row, index (row.model_id)}
		<span class="flex items-center gap-1.5 text-xs">
			<span class="inline-block h-2.5 w-2.5 rounded-full" style:background={row.color}></span>
			<span class="font-mono">{row.model_id}</span>
		</span>
	{/each}
</div>
<p class="text-muted-foreground mt-3 text-center text-xs">{t('bench.chart_radar_note')}</p>
