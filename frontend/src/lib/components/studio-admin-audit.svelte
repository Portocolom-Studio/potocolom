<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import {
		adminErrorMessage,
		buildAuditQuery,
		recheckAdminAccess,
		type AuditFilters
	} from '$lib/studio-admin-logic';
	import { t } from '$lib/i18n.svelte';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';

	type AuditSummary = {
		actions: Record<string, number>;
		gaps: Array<{ action: string; events: number }>;
	};
	type AuditAnomaly = {
		actor_user_id: string;
		distinct_targets: number;
		window_seconds: number;
	};
	type AuditEvent = {
		id: string;
		occurred_at: string;
		actor_user_id: string | null;
		actor_role: string | null;
		action: string;
		target_user_id: string | null;
		object_count: number;
		truncated: boolean;
		severity: string;
	};

	let summary = $state<AuditSummary | null>(null);
	let anomalies = $state<AuditAnomaly[]>([]);
	let events = $state<AuditEvent[]>([]);
	let filters = $state<AuditFilters>({
		actor_user_id: '',
		target_user_id: '',
		action: '',
		limit: '100'
	});
	let activeQuery = $state('limit=100');
	let summaryLoading = $state(true);
	let anomaliesLoading = $state(true);
	let eventsLoading = $state(true);
	let summaryError = $state('');
	let anomaliesError = $state('');
	let eventsError = $state('');
	let exportError = $state('');
	let exporting = $state(false);

	const privilegedActionCount = $derived(
		summary === null ? 0 : Object.values(summary.actions).reduce((total, count) => total + count, 0)
	);
	const gapCount = $derived(
		summary === null ? 0 : summary.gaps.reduce((total, gap) => total + gap.events, 0)
	);

	async function apiError(response: Response): Promise<string> {
		if (response.status === 403) void recheckAdminAccess();
		const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
		return adminErrorMessage(body?.detail, response.statusText);
	}

	function displayDate(value: string): string {
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : value;
	}

	async function loadSummary(): Promise<void> {
		summaryLoading = true;
		summaryError = '';
		try {
			const response = await apiFetch('/api/v1/audit/summary');
			if (!response.ok) {
				summaryError = await apiError(response);
				summary = null;
				return;
			}
			summary = (await response.json()) as AuditSummary;
		} catch {
			summaryError = t('app.admin.request_failed');
		} finally {
			summaryLoading = false;
		}
	}

	async function loadAnomalies(): Promise<void> {
		anomaliesLoading = true;
		anomaliesError = '';
		try {
			const response = await apiFetch('/api/v1/audit/anomalies');
			if (!response.ok) {
				anomaliesError = await apiError(response);
				anomalies = [];
				return;
			}
			anomalies = (await response.json()) as AuditAnomaly[];
		} catch {
			anomaliesError = t('app.admin.request_failed');
		} finally {
			anomaliesLoading = false;
		}
	}

	async function searchAudit(event?: SubmitEvent): Promise<void> {
		event?.preventDefault();
		activeQuery = buildAuditQuery(filters);
		eventsLoading = true;
		eventsError = '';
		try {
			const response = await apiFetch(
				`/api/v1/audit${activeQuery === '' ? '' : `?${activeQuery}`}`
			);
			if (!response.ok) {
				eventsError = await apiError(response);
				events = [];
				return;
			}
			events = (await response.json()) as AuditEvent[];
		} catch {
			eventsError = t('app.admin.request_failed');
		} finally {
			eventsLoading = false;
		}
	}

	async function exportAudit(): Promise<void> {
		exportError = '';
		exporting = true;
		try {
			const response = await apiFetch(
				`/api/v1/audit/export${activeQuery === '' ? '' : `?${activeQuery}`}`
			);
			if (!response.ok) {
				exportError = await apiError(response);
				return;
			}
			const blob = await response.blob();
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = 'audit.json';
			document.body.appendChild(anchor);
			anchor.click();
			anchor.remove();
			// Some browsers cancel a download whose URL is revoked in the same tick.
			setTimeout(() => URL.revokeObjectURL(url), 1000);
		} catch {
			exportError = t('app.admin.request_failed');
		} finally {
			exporting = false;
		}
	}

	onMount(() => {
		void loadSummary();
		void loadAnomalies();
		void searchAudit();
	});
</script>

<div class="flex min-h-0 flex-col gap-4 overflow-auto pb-2">
	<div class="grid gap-4 lg:grid-cols-[minmax(0,1.25fr)_minmax(18rem,0.75fr)]">
		<Card.Root class="p-0 [--card-spacing:0]">
			<Card.Header class="border-border border-b px-4 py-3">
				<Card.Title class="text-base">{t('app.admin.audit_summary')}</Card.Title>
				<Card.Description>{t('app.admin.audit_summary_sub')}</Card.Description>
			</Card.Header>
			<Card.Content class="p-4">
				{#if summaryLoading}
					<p class="text-muted-foreground text-sm">{t('app.admin.loading_summary')}</p>
				{:else if summaryError}
					<p role="alert" class="text-destructive text-sm">{summaryError}</p>
				{:else if summary}
					<div class="flex flex-wrap items-baseline gap-x-8 gap-y-3">
						<div>
							<p class="text-muted-foreground text-xs">{t('app.admin.privileged_actions')}</p>
							<p data-testid="admin-privileged-count" class="text-2xl font-semibold tabular-nums">
								{privilegedActionCount}
							</p>
						</div>
						<div>
							<p class="text-muted-foreground text-xs">{t('app.admin.audit_gaps')}</p>
							<p data-testid="admin-gap-count" class="text-2xl font-semibold tabular-nums">
								{gapCount}
							</p>
						</div>
						{#if summary.gaps.length > 0}
							<ul class="flex flex-wrap gap-x-4 gap-y-1 text-xs">
								{#each summary.gaps as gap (gap.action)}
									<li>{gap.action}: {gap.events}</li>
								{/each}
							</ul>
						{/if}
					</div>
				{/if}
			</Card.Content>
		</Card.Root>

		<Card.Root class="p-0 [--card-spacing:0]">
			<Card.Header class="border-border border-b px-4 py-3">
				<Card.Title class="text-base">{t('app.admin.anomalies')}</Card.Title>
				<Card.Description>{t('app.admin.anomalies_sub')}</Card.Description>
			</Card.Header>
			<Card.Content class="max-h-36 overflow-auto p-4">
				{#if anomaliesLoading}
					<p class="text-muted-foreground text-sm">{t('app.admin.loading_anomalies')}</p>
				{:else if anomaliesError}
					<p role="alert" class="text-destructive text-sm">{anomaliesError}</p>
				{:else if anomalies.length === 0}
					<p class="text-muted-foreground text-sm">{t('app.admin.anomalies_empty')}</p>
				{:else}
					<ul class="flex flex-col gap-2">
						{#each anomalies as anomaly (anomaly.actor_user_id)}
							<li class="text-sm">
								<span class="font-medium">{anomaly.actor_user_id}</span>
								<span class="text-muted-foreground">
									{t('app.admin.anomaly_reads')
										.replace('{count}', String(anomaly.distinct_targets))
										.replace('{minutes}', String(Math.round(anomaly.window_seconds / 60)))}
								</span>
							</li>
						{/each}
					</ul>
				{/if}
			</Card.Content>
		</Card.Root>
	</div>

	<Card.Root class="min-h-0 p-0 [--card-spacing:0]">
		<Card.Header class="border-border border-b px-4 py-3">
			<div class="flex flex-wrap items-center justify-between gap-3">
				<div>
					<Card.Title class="text-base">{t('app.admin.audit_search')}</Card.Title>
					<Card.Description>{t('app.admin.audit_search_sub')}</Card.Description>
				</div>
				<Button
					variant="outline"
					size="sm"
					data-testid="admin-audit-export"
					disabled={exporting}
					onclick={exportAudit}
				>
					{t('app.admin.export')}
				</Button>
			</div>
		</Card.Header>
		<Card.Content class="flex min-h-0 flex-col gap-4 p-4">
			{#if exportError}
				<p role="alert" data-testid="admin-audit-export-error" class="text-destructive text-sm">
					{exportError}
				</p>
			{/if}
			<form
				class="grid gap-3 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_8rem_auto]"
				onsubmit={searchAudit}
			>
				<label class="flex flex-col gap-1 text-xs font-medium">
					{t('app.admin.actor_user_id')}
					<input
						class="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm"
						bind:value={filters.actor_user_id}
					/>
				</label>
				<label class="flex flex-col gap-1 text-xs font-medium">
					{t('app.admin.target_user_id')}
					<input
						class="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm"
						bind:value={filters.target_user_id}
					/>
				</label>
				<label class="flex flex-col gap-1 text-xs font-medium">
					{t('app.admin.action_filter')}
					<input
						class="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm"
						bind:value={filters.action}
					/>
				</label>
				<label class="flex flex-col gap-1 text-xs font-medium">
					{t('app.admin.limit')}
					<input
						class="border-input bg-background h-9 min-w-0 rounded-md border px-3 text-sm"
						type="number"
						min="1"
						max="1000"
						bind:value={filters.limit}
					/>
				</label>
				<div class="flex items-end">
					<Button type="submit" disabled={eventsLoading}>{t('app.admin.search')}</Button>
				</div>
			</form>

			{#if eventsLoading}
				<p class="text-muted-foreground text-sm">{t('app.admin.loading_audit')}</p>
			{:else if eventsError}
				<p role="alert" class="text-destructive text-sm">{eventsError}</p>
			{:else if events.length === 0}
				<p data-testid="admin-audit-count" class="text-muted-foreground text-sm">
					{t('app.admin.audit_empty')}
				</p>
			{:else}
				<div class="border-border min-h-0 overflow-auto rounded-lg border">
					<table class="w-full min-w-[52rem] text-sm">
						<thead class="bg-muted/40 text-muted-foreground sticky top-0 text-left text-xs">
							<tr>
								<th class="px-3 py-2 font-medium">{t('app.admin.occurred_at')}</th>
								<th class="px-3 py-2 font-medium">{t('app.admin.action_column')}</th>
								<th class="px-3 py-2 font-medium">{t('app.admin.actor_user_id')}</th>
								<th class="px-3 py-2 font-medium">{t('app.admin.target_user_id')}</th>
								<th class="px-3 py-2 font-medium">{t('app.admin.severity')}</th>
								<th class="px-3 py-2 font-medium">{t('app.admin.object_count')}</th>
							</tr>
						</thead>
						<tbody>
							{#each events as entry (entry.id)}
								<tr data-testid="admin-audit-row" class="border-border/60 border-t align-top">
									<td class="text-muted-foreground px-3 py-2 text-xs whitespace-nowrap">
										{displayDate(entry.occurred_at)}
									</td>
									<td class="px-3 py-2 font-medium">{entry.action}</td>
									<td class="px-3 py-2 font-mono text-xs">{entry.actor_user_id ?? '-'}</td>
									<td class="px-3 py-2 font-mono text-xs">{entry.target_user_id ?? '-'}</td>
									<td class="px-3 py-2">{entry.severity}</td>
									<td class="px-3 py-2 tabular-nums">{entry.object_count}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
			{#if events.length > 0}
				<p data-testid="admin-audit-count" class="text-muted-foreground text-xs">
					{t('app.admin.audit_count').replace('{count}', String(events.length))}
				</p>
			{/if}
		</Card.Content>
	</Card.Root>
</div>
