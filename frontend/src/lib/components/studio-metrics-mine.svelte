<script lang="ts">
	import { untrack } from 'svelte';
	import { t } from '$lib/i18n.svelte';
	import { account } from '$lib/account.svelte';
	import { accountCacheKey, studioCache } from '$lib/swr-cache';
	import { formatMs } from '$lib/benchmark';
	import type { MetricsRange } from '$lib/studio-metrics-range';
	import {
		getMetricsPreferences,
		saveMetricsPreferences,
		viewOwnerEpoch
	} from '$lib/studio.svelte';
	import {
		byEventsThen,
		categoryShares,
		fetchUsageMe,
		usageMeWindow,
		type UsageMe
	} from '$lib/studio-usage-mine';
	import StudioMetricsRangePicker from '$lib/components/studio-metrics-range-picker.svelte';
	import * as Card from '$lib/components/ui/card';
	import { Skeleton } from '$lib/components/ui/skeleton';

	const ownerEpoch = viewOwnerEpoch();
	const initialRange = getMetricsPreferences().mineRange;
	let range = $state<MetricsRange>(initialRange);
	// Keyed by account: the cache outlives a sign-out in the same tab.
	const usageKey = (range: MetricsRange) =>
		accountCacheKey(account.ownerId, `metrics:usage-me:${range}`);
	let usage = $state<UsageMe | null>(studioCache.read(usageKey(initialRange)) ?? null);
	let loading = $state(true);
	let failed = $state(false);

	$effect(() => {
		const requestEpoch = ownerEpoch;
		const { from, to } = usageMeWindow(range, Date.now());
		const key = usageKey(range);
		let cancelled = false;
		// What this range showed last time stays up while it refreshes.
		const cached = untrack(() => studioCache.read<UsageMe>(key));
		// Never another range's numbers: without a cached copy the skeleton shows.
		usage = cached ?? null;
		loading = true;
		failed = false;
		const controller = new AbortController();
		void fetchUsageMe(from, to, controller.signal).then((body) => {
			if (cancelled || requestEpoch !== viewOwnerEpoch()) return;
			if (body !== null) {
				usage = body;
				studioCache.write(key, body);
			}
			loading = false;
			failed = body === null && usage === null;
		});
		return () => {
			cancelled = true;
			controller.abort();
		};
	});

	$effect(() => {
		const currentRange = range;
		if (ownerEpoch !== viewOwnerEpoch()) return;
		saveMetricsPreferences({ ...getMetricsPreferences(), mineRange: currentRange });
	});

	const shares = $derived(
		usage === null ? [] : categoryShares(usage.by_category, usage.totals.events)
	);
	const models = $derived(
		usage === null ? [] : byEventsThen(usage.by_model, (row) => [row.model_id])
	);
	const modelCategories = $derived(
		usage === null
			? []
			: byEventsThen(usage.by_model_category, (row) => [row.model_id, row.category])
	);
</script>

<div class="flex flex-col gap-6">
	<StudioMetricsRangePicker bind:value={range} />
	{#if loading && usage === null}
		<div role="status" class="flex flex-col gap-6">
			<span class="sr-only">{t('app.metrics.loading')}</span>
			<div class="grid gap-4 sm:grid-cols-3" aria-hidden="true">
				{#each [0, 1, 2] as index (index)}
					<Skeleton class="h-[74px] rounded-[10px]" />
				{/each}
			</div>
			<div class="flex flex-col gap-2" aria-hidden="true">
				{#each [0, 1, 2] as index (index)}
					<Skeleton class="h-2.5 w-full rounded-full" />
				{/each}
			</div>
			<Skeleton class="h-40 rounded-lg" aria-hidden="true" />
		</div>
	{:else if failed || usage === null}
		<p class="text-muted-foreground text-sm">{t('app.metrics.load_failed')}</p>
	{:else if usage.totals.events === 0}
		<p class="text-muted-foreground text-sm">{t('app.metrics.mine_empty')}</p>
	{:else}
		<div class="grid gap-4 sm:grid-cols-3">
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.mine_events')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">{usage.totals.events}</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.mine_gpu_time')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">{formatMs(usage.totals.gpu_ms)}</Card.Title>
				</Card.Header>
			</Card.Root>
			<Card.Root class="gap-1 py-3">
				<Card.Header class="px-4 pb-0">
					<Card.Description>{t('app.metrics.mine_frames')}</Card.Description>
					<Card.Title class="text-2xl tabular-nums">{usage.totals.frames}</Card.Title>
				</Card.Header>
			</Card.Root>
		</div>

		<section class="flex flex-col gap-2">
			<h3 class="text-sm font-medium">{t('app.metrics.mine_category_mix')}</h3>
			<ul class="flex flex-col gap-2">
				{#each shares as row (row.category)}
					<li class="flex items-center gap-3 text-sm">
						<span class="w-24 shrink-0 truncate">{row.category}</span>
						<span class="bg-muted h-2.5 flex-1 overflow-hidden rounded-full">
							<span class="bg-primary block h-full" style={`width: ${row.pct}%`}></span>
						</span>
						<span class="w-24 shrink-0 text-right text-xs tabular-nums">
							{row.events} ({row.pct}%)
						</span>
					</li>
				{/each}
			</ul>
		</section>

		<section class="flex flex-col gap-2">
			<h3 class="text-sm font-medium">{t('app.metrics.mine_models')}</h3>
			<div class="border-border overflow-hidden rounded-lg border">
				<table class="w-full min-w-[28rem] text-sm">
					<thead class="bg-muted/30 text-muted-foreground text-left text-xs">
						<tr>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.col_model')}</th>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.mine_events')}</th>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.col_gpu_avg')}</th>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.mine_col_median')}</th>
						</tr>
					</thead>
					<tbody>
						{#each models as row (row.model_id)}
							<tr class="border-border/60 border-t">
								<td class="px-4 py-2.5 text-xs">{row.model_id}</td>
								<td class="px-4 py-2.5 text-xs tabular-nums">{row.events}</td>
								<td class="px-4 py-2.5 text-xs tabular-nums">{formatMs(row.avg_gpu_ms)}</td>
								<td class="px-4 py-2.5 text-xs tabular-nums">{formatMs(row.p50_duration_ms)}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</section>

		<section class="flex flex-col gap-2">
			<h3 class="text-sm font-medium">{t('app.metrics.mine_model_categories')}</h3>
			<div class="border-border overflow-hidden rounded-lg border">
				<table class="w-full min-w-[28rem] text-sm">
					<thead class="bg-muted/30 text-muted-foreground text-left text-xs">
						<tr>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.col_model')}</th>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.mine_col_category')}</th>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.mine_events')}</th>
							<th class="px-4 py-2.5 font-medium">{t('app.metrics.mine_col_avg_duration')}</th>
						</tr>
					</thead>
					<tbody>
						{#each modelCategories as row (`${row.model_id}\u0000${row.category}`)}
							<tr class="border-border/60 border-t">
								<td class="px-4 py-2.5 text-xs">{row.model_id}</td>
								<td class="px-4 py-2.5 text-xs">{row.category}</td>
								<td class="px-4 py-2.5 text-xs tabular-nums">{row.events}</td>
								<td class="px-4 py-2.5 text-xs tabular-nums">
									{formatMs(row.avg_duration_ms)}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</section>
	{/if}
</div>
