<script lang="ts">
	import { untrack } from 'svelte';
	import { t } from '$lib/i18n.svelte';
	import { account } from '$lib/account.svelte';
	import { accountCacheKey, studioCache } from '$lib/swr-cache';
	import { formatMs } from '$lib/benchmark';
	import { METRICS_RANGE_ORDER, type MetricsRange } from '$lib/studio-metrics-range';
	import { viewOwnerEpoch } from '$lib/studio.svelte';
	import {
		fetchSubjectUsage,
		subjectUsageWindow,
		type SubjectUsage
	} from '$lib/studio-usage-subject';
	import StudioMetricsRangePicker from '$lib/components/studio-metrics-range-picker.svelte';
	import * as Card from '$lib/components/ui/card';
	import { Skeleton } from '$lib/components/ui/skeleton';

	const ownerEpoch = viewOwnerEpoch();
	let range = $state<MetricsRange>('30d');
	const usageKey = (current: MetricsRange) =>
		accountCacheKey(account.ownerId, `metrics:usage-subject:${current}`);
	let usage = $state<SubjectUsage | null>(studioCache.read(usageKey('30d')) ?? null);
	let loading = $state(true);
	let failed = $state(false);
	let unbound = $state(false);

	$effect(() => {
		const requestEpoch = ownerEpoch;
		const { from, to } = subjectUsageWindow(range, Date.now());
		const key = usageKey(range);
		let cancelled = false;
		const cached = untrack(() => studioCache.read<SubjectUsage>(key));
		usage = cached ?? null;
		loading = true;
		failed = false;
		unbound = false;
		const controller = new AbortController();
		void fetchSubjectUsage(from, to, controller.signal).then((body) => {
			if (cancelled || requestEpoch !== viewOwnerEpoch()) return;
			if (body === 'unbound') {
				// A revoked binding answers 404: every cached range belongs to a
				// subject this account may no longer read.
				for (const each of METRICS_RANGE_ORDER) studioCache.delete(usageKey(each));
				usage = null;
				unbound = true;
			} else if (body !== null) {
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
</script>

<div class="flex flex-col gap-6">
	<p class="text-muted-foreground max-w-xl text-sm">{t('app.metrics.subject_intro')}</p>
	<StudioMetricsRangePicker bind:value={range} />
	{#if loading && usage === null}
		<div role="status" class="flex flex-col gap-6">
			<span class="sr-only">{t('app.metrics.loading')}</span>
			<div class="grid gap-4 sm:grid-cols-3" aria-hidden="true">
				{#each [0, 1, 2] as index (index)}
					<Skeleton class="h-[74px] rounded-[10px]" />
				{/each}
			</div>
		</div>
	{:else if failed}
		<p class="text-muted-foreground text-sm">{t('app.metrics.subject_failed')}</p>
	{:else if unbound || usage === null}
		<p class="text-muted-foreground text-sm">{t('app.metrics.subject_unavailable')}</p>
	{:else if usage.totals.events === 0}
		<p class="text-muted-foreground text-sm">{t('app.metrics.subject_empty')}</p>
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
	{/if}
</div>
