<script lang="ts">
	import { cn } from '$lib/utils';
	import { t } from '$lib/i18n.svelte';
	import { type PromptMarqueeItem } from '$lib/prompt-marquee-prompts';

	type Props = {
		prompts: readonly PromptMarqueeItem[];
		direction: 'left' | 'right';
		durationSec?: number;
		paused?: boolean;
		oncopy: (item: PromptMarqueeItem) => void;
	};

	let { prompts, direction, durationSec = 48, paused = false, oncopy }: Props = $props();

	const loop = $derived([...prompts, ...prompts]);
</script>

<div class="prompt-marquee-mask overflow-hidden" role="group">
	<div
		class={cn(
			'prompt-marquee-track flex w-max gap-4',
			direction === 'left' && 'prompt-marquee-left'
		)}
		class:prompt-marquee-right={direction === 'right'}
		class:is-paused={paused}
		style:--marquee-duration="{durationSec}s"
	>
		{#each loop as prompt, index (`${prompt.id}-${index}`)}
			{@const copy = index >= prompts.length}
			<button
				type="button"
				data-prompt
				aria-hidden={copy}
				data-copy={copy ? '' : undefined}
				tabindex={copy ? -1 : undefined}
				class="border-border bg-card/80 hover:border-primary/35 hover:bg-card focus-visible:ring-ring w-80 shrink-0 rounded-2xl border px-4 py-3.5 text-left backdrop-blur-sm transition-colors focus-visible:ring-2 focus-visible:outline-none sm:w-[22rem]"
				onclick={() => oncopy(prompt)}
			>
				<span class="flex flex-col gap-2">
					<span class="text-foreground text-base leading-snug font-semibold sm:text-lg">
						{prompt.primary}
					</span>
					<span class="text-muted-foreground text-sm leading-relaxed">
						{prompt.secondary}
					</span>
					<span class="sr-only">{t('gallery.prompt_copy_label')}</span>
				</span>
			</button>
		{/each}
	</div>
</div>

<style>
	.prompt-marquee-mask {
		mask-image: linear-gradient(to right, transparent, black 8%, black 92%, transparent);
	}

	.prompt-marquee-track {
		will-change: transform;
	}

	.prompt-marquee-left {
		animation: prompt-marquee-left var(--marquee-duration, 48s) linear infinite;
	}

	.prompt-marquee-right {
		animation: prompt-marquee-right var(--marquee-duration, 48s) linear infinite;
	}

	/* Hover, focus and the pause control all stop the motion here, in CSS. */
	.prompt-marquee-track.is-paused,
	.prompt-marquee-mask:hover .prompt-marquee-track,
	.prompt-marquee-track:focus-within {
		animation-play-state: paused;
	}

	@keyframes prompt-marquee-left {
		from {
			transform: translateX(0);
		}
		to {
			transform: translateX(-50%);
		}
	}

	@keyframes prompt-marquee-right {
		from {
			transform: translateX(-50%);
		}
		to {
			transform: translateX(0);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.prompt-marquee-left,
		.prompt-marquee-right {
			animation: none;
			transform: none;
			flex-wrap: wrap;
			width: 100%;
			justify-content: center;
		}

		/* A static strip shows each prompt once. */
		[data-copy] {
			display: none;
		}
	}
</style>
