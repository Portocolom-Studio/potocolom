<script lang="ts">
	import { t } from '$lib/i18n.svelte';

	const steps = [
		{
			title: 'lineage.step1_title',
			body: 'lineage.step1_body',
			alt: 'lineage.alt1',
			src: '/images/landing/lineage-generate.webp',
			size: 150,
			link: 'lineage.link1'
		},
		{
			title: 'lineage.step2_title',
			body: 'lineage.step2_body',
			alt: 'lineage.alt2',
			src: '/images/landing/lineage-edit.webp',
			size: 200,
			link: 'lineage.link2'
		},
		{
			title: 'lineage.step3_title',
			body: 'lineage.step3_body',
			alt: 'lineage.alt3',
			src: '/images/landing/lineage-upscale.webp',
			size: 280,
			link: null
		}
	] as const;
</script>

<section id="lineage" class="lineage" aria-labelledby="lineage-title">
	<div class="head">
		<h2 id="lineage-title">{t('lineage.title')}</h2>
		<p>{t('lineage.body')}</p>
	</div>

	<ol class="row">
		{#each steps as step (step.title)}
			<li class="step" style="--size: {step.size}px">
				<figure>
					<figcaption>
						<strong>{t(step.title)}</strong>
						<span>{t(step.body)}</span>
					</figcaption>
					<div class="frame">
						<img
							src={step.src}
							width="1024"
							height="1024"
							loading="lazy"
							decoding="async"
							alt={t(step.alt)}
						/>
						{#if step.link}
							<span class="link">
								<span class="chip">{t(step.link)}</span>
							</span>
						{/if}
					</div>
				</figure>
			</li>
		{/each}
	</ol>
</section>

<style>
	/* Captions sit above the frames so the chip after each image lands
	   between two figures in the phone stack. */
	.lineage {
		max-width: 1344px;
		margin-inline: auto;
		padding: clamp(4.5rem, 9vw, 8rem) 3rem;
	}

	.head {
		display: flex;
		justify-content: space-between;
		align-items: flex-end;
		gap: 2rem;
		margin-bottom: clamp(3rem, 6vw, 4.5rem);
	}

	h2 {
		max-width: 16ch;
		font-size: clamp(2.25rem, 3.4vw, 2.75rem);
		line-height: 1.02;
		text-wrap: balance;
	}

	.head p {
		max-width: 28rem;
		color: var(--k-muted);
		font-size: 1.125rem;
		line-height: 1.5;
	}

	.row {
		--gap: clamp(2rem, 12vw, 12.5rem);
		display: flex;
		align-items: flex-end;
		min-width: 0;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.step {
		position: relative;
		flex: 0 1 var(--size);
		min-width: 0;
	}

	.step:not(:last-child) {
		margin-inline-end: var(--gap);
	}

	figure {
		display: grid;
		gap: 0.875rem;
		margin: 0;
	}

	figcaption {
		display: grid;
		gap: 0.2rem;
	}

	figcaption strong {
		font-size: 1.0625rem;
		font-weight: 700;
		line-height: 1.25;
	}

	figcaption span {
		color: var(--k-muted);
		font-size: 0.9375rem;
		line-height: 1.4;
	}

	.frame {
		position: relative;
	}

	.frame img {
		display: block;
		width: 100%;
		height: auto;
		aspect-ratio: 1 / 1;
		border-radius: 8px;
		object-fit: cover;
	}

	/* On desktop the connector runs from the frame edge across the gap and
	   sits at the middle of the image. */
	.link {
		position: absolute;
		top: 50%;
		left: 100%;
		width: var(--gap);
		border-block-start: 1px solid var(--k-line);
	}

	.chip {
		position: absolute;
		top: 0;
		left: 50%;
		width: max-content;
		max-width: calc(var(--gap) - 0.75rem);
		padding: 0.3rem 0.6rem;
		border: 1px solid var(--k-line);
		border-radius: 6px;
		background: var(--k-panel);
		color: var(--k-muted);
		font-size: 0.8125rem;
		font-weight: 600;
		line-height: 1.25;
		text-align: center;
		transform: translate(-50%, -50%);
	}

	@media (max-width: 860px) {
		.lineage {
			padding-inline: 1.25rem;
		}

		.head {
			flex-direction: column;
			align-items: flex-start;
			gap: 1.25rem;
		}

		.row {
			flex-direction: column;
			align-items: stretch;
		}

		.step {
			flex-basis: auto;
		}

		.step:not(:last-child) {
			margin-inline-end: 0;
			margin-block-end: 2rem;
		}

		.link {
			position: static;
			width: auto;
			margin-block-start: 0.875rem;
			border-block-start: 0;
			text-align: center;
		}

		.chip {
			position: static;
			display: inline-block;
			width: auto;
			max-width: none;
			transform: none;
			white-space: nowrap;
		}
	}
</style>
