<script lang="ts">
	import type { Snippet } from 'svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';

	// The frame every first-run step shares: the step marks, the heading, the
	// step's content and its footer. The heading is a snippet because the setup
	// page uses an h1 and the studio card a Dialog.Title.
	let {
		step,
		heading,
		description,
		children,
		footer
	}: {
		step: number;
		heading: Snippet;
		description?: string;
		children?: Snippet;
		footer?: Snippet;
	} = $props();

	const TOTAL = 4;
	const label = $derived(
		t('first_run.step_of').replace('{current}', String(step)).replace('{total}', String(TOTAL))
	);
</script>

<div class="flex min-h-0 flex-col gap-6" data-testid="first-run-frame" data-step={step}>
	<div class="flex items-center justify-between gap-4">
		<span class="text-base font-bold tracking-tight">potocolom</span>
		<LanguageToggle />
	</div>
	<div class="flex items-center gap-2" data-testid="first-run-steps">
		<ol class="flex items-center gap-2" aria-label={label}>
			{#each { length: TOTAL } as _, index (index)}
				<li
					class={[
						'h-1 rounded-full',
						index + 1 === step ? 'bg-foreground w-10' : 'w-6',
						index + 1 < step ? 'bg-muted-foreground' : '',
						index + 1 > step ? 'bg-hover' : ''
					]}
					aria-current={index + 1 === step ? 'step' : undefined}
				></li>
			{/each}
		</ol>
		<span class="text-muted-foreground ml-2 text-xs" aria-hidden="true">{label}</span>
	</div>
	<div class="flex flex-col gap-2">
		{@render heading()}
		{#if description}
			<p class="text-muted-foreground text-sm">{description}</p>
		{/if}
	</div>
	{#if children}
		<div class="min-h-0">{@render children()}</div>
	{/if}
	{#if footer}
		<div class="flex flex-wrap items-center gap-2">{@render footer()}</div>
	{/if}
</div>
