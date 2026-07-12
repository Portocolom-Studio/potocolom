<script lang="ts">
	import { fade } from 'svelte/transition';
	import PromptMarqueeRow from '$lib/components/PromptMarqueeRow.svelte';
	import { promptMarqueePrompts, promptMarqueeText } from '$lib/prompt-marquee-prompts';
	import { t } from '$lib/i18n.svelte';
	import CheckIcon from '@lucide/svelte/icons/check';

	const rowA = promptMarqueePrompts.slice(0, 8);
	const rowB = [...promptMarqueePrompts.slice(8)].reverse();

	let showCopied = $state(false);
	let copiedTimeout: ReturnType<typeof setTimeout> | undefined;

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

<div class="mt-12">
	<p class="text-muted-foreground text-center text-sm">{t('gallery.prompts_hint')}</p>
	<div
		class="relative left-1/2 mt-5 flex w-screen -translate-x-1/2 flex-col gap-4"
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

		<PromptMarqueeRow prompts={rowA} direction="left" durationSec={52} oncopy={copyPrompt} />
		<PromptMarqueeRow prompts={rowB} direction="right" durationSec={44} oncopy={copyPrompt} />
	</div>
</div>
