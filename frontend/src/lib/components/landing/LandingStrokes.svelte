<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import ChevronsLeftRightIcon from '@lucide/svelte/icons/chevrons-left-right';

	let position = $state(46);
</script>

<section id="strokes" class="strokes" aria-labelledby="strokes-title">
	<div class="copy">
		<h2 id="strokes-title">{t('strokes.title')}</h2>
		<p class="body">{t('strokes.body')}</p>
		<ul class="facts">
			<li>
				<strong>{t('strokes.fact1_title')}</strong>
				<span>{t('strokes.fact1_body')}</span>
			</li>
			<li>
				<strong>{t('strokes.fact2_title')}</strong>
				<span>{t('strokes.fact2_body')}</span>
			</li>
		</ul>
	</div>

	<div class="compare" style="--pos: {position}%">
		<input
			class="range"
			type="range"
			min="0"
			max="100"
			bind:value={position}
			aria-label={t('strokes.compare_label')}
		/>
		<img
			class="result"
			src="/images/landing/strokes-result.webp"
			width="1024"
			height="1024"
			loading="lazy"
			decoding="async"
			alt={t('strokes.alt_after')}
		/>
		<img
			class="sketch"
			src="/images/landing/strokes-sketch.webp"
			width="1024"
			height="1024"
			loading="lazy"
			decoding="async"
			alt={t('strokes.alt_before')}
		/>
		<div class="divider" aria-hidden="true">
			<span class="knob">
				<ChevronsLeftRightIcon />
			</span>
		</div>
		<span class="tag tag-before" aria-hidden="true">{t('strokes.label_before')}</span>
		<span class="tag tag-after" aria-hidden="true">{t('strokes.label_after')}</span>
	</div>
</section>

<style>
	/* The range input covers the frame so keyboard and touch work natively.
	   The knob shows the focus ring because the input itself is transparent. */
	.strokes {
		display: grid;
		grid-template-columns: minmax(0, 0.7fr) minmax(0, 1fr);
		gap: 4rem;
		align-items: end;
		max-width: 78rem;
		margin-inline: auto;
		padding: clamp(3.5rem, 9vw, 6.5rem) clamp(1rem, 4vw, 3rem);
	}

	.copy {
		min-width: 0;
	}

	h2 {
		max-width: 16ch;
		font-size: clamp(2.25rem, 3.4vw, 2.75rem);
		line-height: 1.02;
		text-wrap: balance;
	}

	.body {
		max-width: 30rem;
		margin-block: 1.75rem 2.75rem;
		color: var(--k-muted);
		font-size: 1.125rem;
		line-height: 1.5;
	}

	.facts {
		display: grid;
		gap: 1.375rem;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.facts li {
		display: grid;
		gap: 0.25rem;
	}

	.facts strong {
		font-size: 1.5rem;
		font-weight: 600;
		line-height: 1.15;
		letter-spacing: -0.03em;
	}

	.facts span {
		color: var(--k-muted);
		font-size: 0.9375rem;
	}

	.compare {
		position: relative;
		width: 100%;
		max-width: 760px;
		aspect-ratio: 1 / 1;
		justify-self: end;
		min-width: 0;
		overflow: hidden;
		border: 1px solid var(--k-line);
		border-radius: 12px;
		background: var(--k-panel);
	}

	.range {
		position: absolute;
		inset: 0;
		z-index: 2;
		width: 100%;
		height: 100%;
		margin: 0;
		opacity: 0;
		cursor: ew-resize;
		touch-action: pan-y;
	}

	.compare img {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		pointer-events: none;
		user-select: none;
	}

	.sketch {
		clip-path: inset(0 calc(100% - var(--pos)) 0 0);
	}

	.divider {
		position: absolute;
		inset-block: 0;
		left: var(--pos);
		width: 2px;
		margin-inline-start: -1px;
		background: var(--k-ink);
		pointer-events: none;
	}

	.knob {
		position: absolute;
		top: 50%;
		left: 50%;
		display: grid;
		width: 2rem;
		height: 2rem;
		place-items: center;
		border-radius: 50%;
		background: var(--k-ink);
		color: var(--k-paper);
		transform: translate(-50%, -50%);
	}

	.knob :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	.range:focus-visible ~ .divider .knob {
		outline: 2px solid var(--k-focus);
		outline-offset: 3px;
	}

	.tag {
		position: absolute;
		bottom: 1rem;
		z-index: 1;
		padding: 0.3rem 0.65rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		color: var(--k-ink);
		font-size: 0.75rem;
		font-weight: 600;
		line-height: 1.2;
		white-space: nowrap;
		pointer-events: none;
	}

	.tag-before {
		left: 1rem;
	}

	.tag-after {
		right: 1rem;
	}

	@media (max-width: 860px) {
		.strokes {
			grid-template-columns: minmax(0, 1fr);
			gap: 2.25rem;
			align-items: start;
			padding-inline: 1.25rem;
		}

		.compare {
			justify-self: stretch;
			max-width: none;
		}
	}
</style>
