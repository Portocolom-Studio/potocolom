<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { account } from '$lib/account.svelte';
	import { t } from '$lib/i18n.svelte';
	import { openAdmin, studio } from '$lib/studio.svelte';
	import { Button } from '$lib/components/ui/button';
	import {
		defaultGpuOption,
		gpuCardOpensFleet,
		markGpuCardDone,
		shouldShowGpuCard,
		type GpuOption
	} from '$lib/gpu-setup-card';
	import { Dialog } from 'bits-ui';

	// Decided once, at mount, before the models answer: the tour waits on
	// studio.gpuCardOpen, so the flag must already say so by then.
	studio.gpuCardOpen = shouldShowGpuCard(
		account.localModeConfirmed || account.current?.role === 'admin',
		() => localStorage
	);

	// Null while the list loads and when it failed. Both keep the "this" option.
	let workers = $state<string[] | null>(null);
	let loading = $state(true);
	// The user's own pick wins over the default, which may still change while loading.
	let choice = $state<GpuOption | null>(null);
	const option = $derived(choice ?? defaultGpuOption(workers));

	onMount(() => {
		if (studio.gpuCardOpen) void loadWorkers();
	});

	async function loadWorkers(): Promise<void> {
		try {
			const response = await apiFetch('/api/v1/admin/models', {
				signal: AbortSignal.timeout(10_000)
			});
			if (response.ok) {
				const list = (await response.json()) as { worker_id: string }[];
				workers = [...new Set(list.map((worker) => worker.worker_id))];
			}
		} catch {
			// A failed or slow list shows the card with the "could not be read" note.
		} finally {
			loading = false;
		}
	}

	function finish(ending: GpuOption | 'dismissed'): void {
		if (!studio.gpuCardOpen) return;
		markGpuCardDone(() => localStorage);
		if (gpuCardOpensFleet(ending)) {
			studio.fleetAddRequested = true;
			openAdmin('fleet');
		}
		studio.gpuCardOpen = false;
	}
</script>

<Dialog.Root
	open={studio.gpuCardOpen}
	onOpenChange={(open) => {
		// Escape reaches the tour's window handler in the same keystroke that closes
		// the card. Finishing in a later task keeps that keystroke from also skipping
		// the tour that the card's end starts.
		if (!open) setTimeout(() => finish('dismissed'));
	}}
>
	<Dialog.Portal>
		<Dialog.Overlay class="bg-background/80 fixed inset-0 z-50 backdrop-blur-sm" />
		<Dialog.Content
			data-testid="gpu-card"
			class="bg-popover text-popover-foreground fixed top-1/2 left-1/2 z-50 max-h-[90vh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border p-6 shadow-lg"
		>
			<Dialog.Title class="text-lg font-semibold">{t('app.gpu_card.title')}</Dialog.Title>
			<Dialog.Description class="text-muted-foreground mt-2 text-sm">
				{t('app.gpu_card.body')}
			</Dialog.Description>

			<form
				class="mt-4 flex flex-col gap-4"
				onsubmit={(event) => {
					event.preventDefault();
					finish(option);
				}}
			>
				<fieldset class="flex flex-col gap-3">
					<legend class="mb-2 text-sm font-medium">{t('app.gpu_card.group_label')}</legend>
					<label class="border-border flex items-start gap-3 rounded-md border p-3">
						<input
							type="radio"
							name="gpu-location"
							value="this"
							class="mt-1"
							data-testid="gpu-card-this"
							checked={option === 'this'}
							onchange={() => (choice = 'this')}
						/>
						<span class="flex flex-col gap-1">
							<span class="text-sm font-medium">{t('app.gpu_card.this_label')}</span>
							<span class="text-muted-foreground text-xs">{t('app.gpu_card.this_hint')}</span>
						</span>
					</label>
					{#if option === 'this'}
						<div class="text-sm" data-testid="gpu-card-workers">
							{#if loading}
								<p role="status" class="text-muted-foreground">{t('app.gpu_card.loading')}</p>
							{:else if workers === null}
								<p role="alert" class="text-destructive">{t('app.gpu_card.unreadable')}</p>
							{:else if workers.length === 0}
								<p class="text-muted-foreground">{t('app.gpu_card.none_connected')}</p>
							{:else}
								<p class="text-muted-foreground mb-1">{t('app.gpu_card.connected_title')}</p>
								<ul class="flex flex-col gap-1">
									{#each workers as worker (worker)}
										<li class="font-mono text-xs break-all" data-testid="gpu-card-worker">
											{worker}
										</li>
									{/each}
								</ul>
							{/if}
						</div>
					{/if}
					<label class="border-border flex items-start gap-3 rounded-md border p-3">
						<input
							type="radio"
							name="gpu-location"
							value="other"
							class="mt-1"
							data-testid="gpu-card-other"
							checked={option === 'other'}
							onchange={() => (choice = 'other')}
						/>
						<span class="flex flex-col gap-1">
							<span class="text-sm font-medium">{t('app.gpu_card.other_label')}</span>
							<span class="text-muted-foreground text-xs">{t('app.gpu_card.other_hint')}</span>
						</span>
					</label>
				</fieldset>

				<div class="flex justify-end">
					<Button type="submit" data-testid="gpu-card-continue">{t('app.gpu_card.continue')}</Button
					>
				</div>
			</form>
		</Dialog.Content>
	</Dialog.Portal>
</Dialog.Root>
