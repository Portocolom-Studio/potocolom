<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';

	const items = [
		{ question: 'faq.q1', answer: 'faq.a1' },
		{ question: 'faq.q2', answer: 'faq.a2' },
		{ question: 'faq.q3', answer: 'faq.a3' },
		{ question: 'faq.q4', answer: 'faq.a4' },
		{ question: 'faq.q5', answer: 'faq.a5' }
	] as const;
</script>

<section class="faq" aria-labelledby="faq-title">
	<div class="heading">
		<h2 id="faq-title">{t('faq.title')}</h2>
		<p>{t('faq.sub')}</p>
	</div>

	<div class="questions">
		{#each items as item, index (item.question)}
			<details name="landing-faq">
				<summary>
					<span class="number" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
					<span>{t(item.question)}</span>
					<span class="toggle" aria-hidden="true">
						<ChevronDownIcon />
					</span>
				</summary>
				<p>{t(item.answer)}</p>
			</details>
		{/each}
	</div>
</section>

<style>
	/* Rule-based rows keep the FAQ quiet between the particle split and
	   the more atmospheric waitlist field. */
	.faq {
		position: relative;
		z-index: 1;
		display: grid;
		grid-template-columns: minmax(0, 0.72fr) minmax(0, 1.28fr);
		gap: clamp(2.5rem, 8vw, 8rem);
		max-width: 78rem;
		margin-inline: auto;
		padding: clamp(4.5rem, 10vw, 8rem) clamp(1rem, 4vw, 3rem);
		border-block: 1px solid var(--k-line);
	}

	.heading {
		display: grid;
		align-content: start;
		gap: 1.25rem;
	}

	h2 {
		max-width: 8ch;
		font-size: clamp(2.6rem, 6vw, 5.5rem);
		line-height: 0.94;
	}

	.heading p {
		max-width: 34ch;
		color: var(--k-muted);
		font-size: 1rem;
	}

	.questions {
		min-width: 0;
		border-block-start: 1px solid var(--k-line);
	}

	details {
		border-block-end: 1px solid var(--k-line);
	}

	summary {
		display: grid;
		grid-template-columns: 2rem minmax(0, 1fr) 2.25rem;
		align-items: center;
		gap: clamp(0.75rem, 2vw, 1.25rem);
		min-height: 5.25rem;
		padding-block: 1rem;
		cursor: pointer;
		list-style: none;
		font-size: clamp(1.05rem, 1.8vw, 1.3rem);
		font-weight: 700;
		line-height: 1.25;
	}

	summary::-webkit-details-marker {
		display: none;
	}

	summary:focus-visible {
		outline: 2px solid var(--k-accent);
		outline-offset: 4px;
	}

	.number {
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.72rem;
		font-weight: 500;
		letter-spacing: 0.04em;
	}

	.toggle {
		display: grid;
		width: 2.25rem;
		aspect-ratio: 1;
		place-items: center;
		border: 1px solid var(--k-line);
		border-radius: 50%;
		color: var(--k-muted);
		transition:
			border-color 180ms var(--k-ease),
			color 180ms var(--k-ease),
			transform 240ms var(--k-ease);
	}

	.toggle :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	details[open] .toggle {
		border-color: var(--k-accent);
		color: var(--k-accent);
		transform: rotate(180deg);
	}

	details > p {
		max-width: 62ch;
		padding: 0 3.5rem 1.75rem 3.25rem;
		color: var(--k-muted);
		font-size: 0.98rem;
	}

	@media (hover: hover) and (pointer: fine) {
		summary:hover .toggle {
			border-color: var(--k-accent);
			color: var(--k-accent);
		}
	}

	summary:active .toggle {
		transform: scale(0.94);
	}

	details[open] summary:active .toggle {
		transform: rotate(180deg) scale(0.94);
	}

	@media (max-width: 48rem) {
		.faq {
			grid-template-columns: minmax(0, 1fr);
			gap: 2.75rem;
		}

		h2 {
			max-width: 12ch;
		}
	}

	@media (max-width: 30rem) {
		summary {
			grid-template-columns: minmax(0, 1fr) 2.25rem;
		}

		.number {
			display: none;
		}

		details > p {
			padding-inline: 0 3rem;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.toggle {
			transition: none;
		}
	}
</style>
