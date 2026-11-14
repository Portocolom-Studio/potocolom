<script lang="ts">
	import { fade } from 'svelte/transition';
	import PromptMarqueeRow from '$lib/components/PromptMarqueeRow.svelte';
	import { Button } from '$lib/components/ui/button';
	import { promptMarqueePrompts, promptMarqueeText } from '$lib/prompt-marquee-prompts';
	import { t } from '$lib/i18n.svelte';
	import CheckIcon from '@lucide/svelte/icons/check';
	import PauseIcon from '@lucide/svelte/icons/pause';
	import PlayIcon from '@lucide/svelte/icons/play';

	const rowA = promptMarqueePrompts.slice(0, 8);
	const rowB = [...promptMarqueePrompts.slice(8)].reverse();

	let showCopied = $state(false);
	let copiedTimeout: ReturnType<typeof setTimeout> | undefined;
	let motionPaused = $state(false);

	async function copyPrompt(item: (typeof promptMarqueePrompts)[number]) {
		try {
			await navigator.clipboard.writeText(promptMarqueeText(item));
			showCopied = true;
			clearTimeout(copiedTimeout);
			copiedTimeout = setTimeout(() => {
				showCopied = false;
			}, 1800);
		} catch {
			// Clipboard can fail on insecure contexts or denied permissions.
		}
	}
</script>

<div class="mt-12 overflow-x-clip">
	<p class="prompt-hint text-muted-foreground px-4 text-center text-sm sm:px-6">
		{t('gallery.prompts_hint')}
	</p>
	<div
		class="relative mt-5 flex w-full flex-col gap-4 overflow-x-clip"
		aria-label={t('gallery.prompts_aria')}
	>
		{#if showCopied}
			<div
				class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center"
				transition:fade={{ duration: 150 }}
			>
				<div
					class="bg-popover text-popover-foreground border-border flex items-center gap-2 rounded-xl border px-4 py-2.5 text-sm font-medium shadow-lg"
					role="status"
					aria-live="polite"
				>
					<CheckIcon class="text-primary size-4" />
					{t('gallery.prompt_copied')}
				</div>
			</div>
		{/if}

		<PromptMarqueeRow
			prompts={rowA}
			direction="left"
			durationSec={52}
			paused={motionPaused}
			oncopy={copyPrompt}
		/>
		<PromptMarqueeRow
			prompts={rowB}
			direction="right"
			durationSec={44}
			paused={motionPaused}
			oncopy={copyPrompt}
		/>
		<Button
			variant="outline"
			size="icon-lg"
			class="me-4 self-end sm:me-6"
			aria-pressed={motionPaused}
			aria-label={t('gallery.prompts_pause')}
			onclick={() => (motionPaused = !motionPaused)}
		>
			{#if motionPaused}
				<PlayIcon aria-hidden="true" />
			{:else}
				<PauseIcon aria-hidden="true" />
			{/if}
		</Button>
	</div>
</div>

<style>
	/* The light landing paper needs the landing muted token (--k-muted). Dark keeps the global muted token. */
	:global(:root[data-landing-mode='light']) .prompt-hint {
		color: var(--k-muted);
	}
</style>
