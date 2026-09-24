<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import type { IllusionCandidateItem, IllusionGalleryItem } from '$lib/illusion-public-facts';

	let {
		item,
		showPrime = false,
		priority = false
	}: {
		item: IllusionGalleryItem | IllusionCandidateItem;
		showPrime?: boolean;
		priority?: boolean;
	} = $props();

	let flipped = $state(false);

	const liveLabel = $derived(flipped ? t(item.invertedKey) : t(item.uprightKey));
	const viewAlt = $derived(`${t(item.titleKey)}: ${liveLabel}`);

	function toggle(): void {
		flipped = !flipped;
	}
</script>

<figure class={['flip', showPrime && 'with-prime']}>
	<!-- The sheet is a second, pointer-only way to turn the card; the Flip button is the keyboard one. -->
	<button type="button" class="sheet" tabindex="-1" aria-label={viewAlt} onclick={toggle}>
		<img
			class={{ turned: flipped }}
			src={item.view}
			alt=""
			width={item.viewWidth}
			height={item.viewHeight}
			loading={priority ? 'eager' : 'lazy'}
			decoding="async"
		/>
	</button>
	{#if showPrime}
		<div class="prime">
			<img
				src={item.prime}
				alt={t('ill.prime_label')}
				width={item.primeWidth}
				height={item.primeHeight}
				loading={priority ? 'eager' : 'lazy'}
				decoding="async"
			/>
			<p>{t('ill.prime_label')}</p>
		</div>
	{/if}
	<figcaption>
		<p class="pair">{t(item.titleKey)}</p>
		{#if showPrime}
			<p class="live">{liveLabel}</p>
		{/if}
		<button type="button" aria-pressed={flipped} onclick={toggle}>
			{flipped ? t('ill.flipped') : t('ill.flip')}
		</button>
	</figcaption>
</figure>

<style>
	/* Hallmark - one sheet, one 180 turn - genre: spare - contrast: pass - mobile: pass */
	.flip {
		display: grid;
		gap: 0.75rem;
		margin: 0;
		min-width: 0;
	}

	.with-prime {
		grid-template-columns: minmax(0, 1fr);
	}

	.sheet {
		display: block;
		width: 100%;
		min-width: 0;
		padding: 0;
		overflow: clip;
		cursor: pointer;
		border: 1px solid var(--k-line);
		border-radius: 1rem;
		background: oklch(0.99 0.003 255);
	}

	.sheet img,
	.prime img {
		display: block;
		width: 100%;
		height: auto;
	}

	.sheet img {
		transform-origin: center;
		transition: transform 420ms var(--k-ease);
	}

	.sheet img.turned {
		transform: rotate(180deg);
	}

	figcaption {
		display: grid;
		justify-items: start;
		gap: 0.25rem;
	}

	.pair {
		color: var(--k-ink);
		font-size: 0.95rem;
	}

	.live {
		color: var(--k-muted);
		font-size: 0.82rem;
	}

	figcaption button {
		margin-block-start: 0.35rem;
		padding: 0.35rem 0.9rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: transparent;
		color: var(--k-ink);
		font-size: 0.82rem;
		cursor: pointer;
	}

	figcaption button:hover,
	figcaption button[aria-pressed='true'] {
		border-color: var(--k-accent);
	}

	figcaption button:focus-visible {
		outline: 2px solid var(--k-accent);
		outline-offset: 2px;
	}

	.prime {
		display: grid;
		gap: 0.4rem;
		max-width: 12rem;
	}

	.prime img {
		border: 1px solid var(--k-line);
		border-radius: 0.6rem;
		background: oklch(0.99 0.003 255);
	}

	.prime p {
		color: var(--k-muted);
		font-size: 0.78rem;
	}

	@media (min-width: 48rem) {
		.with-prime {
			grid-template-columns: minmax(0, 1.4fr) minmax(0, 0.7fr);
			align-items: start;
		}

		.with-prime figcaption {
			grid-column: 1;
		}

		.with-prime .prime {
			grid-column: 2;
			grid-row: 1;
			max-width: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.sheet img {
			transition: none;
		}
	}
</style>
