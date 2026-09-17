<script lang="ts">
	import { resolve } from '$app/paths';
	import IllusionFlip from '$lib/components/IllusionFlip.svelte';
	import LatentShell from '$lib/components/landing/LatentShell.svelte';
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import { t } from '$lib/i18n.svelte';
	import {
		fillIllusionCopy,
		ILLUSION_CANDIDATES,
		ILLUSION_GALLERY,
		ILLUSION_HERO,
		ILLUSION_PAPER_URL
	} from '$lib/illusion-public-facts';
	import '../../landing-tokens.css';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	const sections = [
		{ id: 's8', title: 'ill.s8_title', paragraphs: ['ill.s8_p1'], gallery: true },
		{ id: 's11', title: 'ill.s11_title', paragraphs: ['ill.s11_p1'], candidates: true },
		{ id: 's1', title: 'ill.s1_title', paragraphs: ['ill.s1_p1', 'ill.s1_p2', 'ill.s1_p3'] },
		{
			id: 's2',
			title: 'ill.s2_title',
			paragraphs: ['ill.s2_p1', 'ill.s2_p2', 'ill.s2_p3'],
			workedExample: true,
			figures: [
				{
					src: '/illusions/architecture.webp',
					cap: 'ill.fig_arch_cap',
					width: 2560,
					height: 1440
				},
				{
					src: '/illusions/workflow.webp',
					cap: 'ill.fig_workflow_cap',
					width: 2560,
					height: 1440
				}
			]
		},
		{
			id: 's3',
			title: 'ill.s3_title',
			paragraphs: ['ill.s3_p1', 'ill.s3_p2'],
			figures: [
				{
					src: '/illusions/ffn.webp',
					cap: 'ill.fig_ffn_cap',
					width: 2560,
					height: 1440
				}
			]
		},
		{
			id: 's4',
			title: 'ill.s4_title',
			paragraphs: ['ill.s4_p1', 'ill.s4_p2'],
			figures: [
				{
					src: '/illusions/sds.webp',
					cap: 'ill.fig_sds_cap',
					width: 2560,
					height: 1600
				},
				{
					src: '/illusions/symbols.webp',
					cap: 'ill.fig_symbols_cap',
					width: 2560,
					height: 1680
				}
			]
		},
		{
			id: 's5',
			title: 'ill.s5_title',
			paragraphs: ['ill.s5_p1', 'ill.s5_p2', 'ill.s5_p3'],
			figures: [
				{
					src: '/illusions/two-phase.webp',
					cap: 'ill.fig_loop_cap',
					width: 2560,
					height: 1560
				},
				{
					src: '/illusions/dream.webp',
					cap: 'ill.fig_dream_cap',
					width: 2560,
					height: 1440
				}
			]
		},
		{
			id: 's6',
			title: 'ill.s6_title',
			paragraphs: ['ill.s6_p1', 'ill.s6_p2'],
			figures: [
				{
					src: '/illusions/joint.webp',
					cap: 'ill.fig_joint_cap',
					width: 1520,
					height: 2680
				}
			]
		},
		{
			id: 's7',
			title: 'ill.s7_title',
			paragraphs: ['ill.s7_p1', 'ill.s7_p2', 'ill.s7_p3', 'ill.s7_p4'],
			figures: [
				{
					src: '/illusions/recipe.webp',
					cap: 'ill.fig_recipe_cap',
					width: 2560,
					height: 1600
				},
				{
					src: '/illusions/review.webp',
					cap: 'ill.fig_review_cap',
					width: 2560,
					height: 1560
				},
				{
					src: '/illusions/seeds.webp',
					cap: 'ill.fig_seeds_cap',
					width: 2560,
					height: 1440
				},
				{
					src: '/illusions/failures.webp',
					cap: 'ill.fig_failures_cap',
					width: 2560,
					height: 1312
				}
			]
		},
		{
			id: 's9',
			title: 'ill.s9_title',
			paragraphs: ['ill.s9_p1'],
			figures: [
				{
					src: '/illusions/print.webp',
					cap: 'ill.fig_print_cap',
					width: 2560,
					height: 1440
				}
			]
		},
		{ id: 's10', title: 'ill.s10_title', paragraphs: ['ill.s10_p1', 'ill.s10_p2', 'ill.s10_p3'] }
	] as const;
</script>

<Seo
	title="Diffusion Illusions | One Sheet, Two Pictures | potocolom"
	description="Print a prime image. Turn the sheet. A second subject appears. Research on the same worker stack as the studio. Not a product feature yet."
	path="/illusions"
/>

<LatentShell current="illusions">
	<main>
		<section class="opening">
			<h1>{t('ill.title')}</h1>
			<p class="lede">{t('ill.sub')}</p>
			<IllusionFlip item={ILLUSION_HERO} showPrime priority />
			<p class="hint">{t('ill.hero_hint')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/whitepaper')}>{t('ill.cta_whitepaper')}</a>
				<a class="pill pill-ghost" href={ILLUSION_PAPER_URL}>{t('ill.cta_paper')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('ill.cta_github')}</a>
			</div>
		</section>

		<div class="panel document">
			<aside aria-label={t('ill.toc')}>
				<p class="rail-label">{t('ill.toc')}</p>
				<ol>
					{#each sections as section (section.id)}
						<li><a href="#{section.id}">{t(section.title)}</a></li>
					{/each}
				</ol>
			</aside>

			<article>
				{#each sections as section (section.id)}
					<section id={section.id}>
						<h2>{t(section.title)}</h2>
						{#each section.paragraphs as paragraph (paragraph)}
							<p>{fillIllusionCopy(t(paragraph))}</p>
						{/each}
						{#if 'gallery' in section}
							<div class="gallery">
								{#each ILLUSION_GALLERY as item (item.id)}
									<IllusionFlip {item} meta="keeper" />
								{/each}
							</div>
						{/if}
						{#if 'candidates' in section}
							<div class="gallery">
								{#each ILLUSION_CANDIDATES as item (item.id)}
									<IllusionFlip {item} meta="candidate" />
								{/each}
							</div>
						{/if}
						{#if 'workedExample' in section}
							<div class="worked">
								<figure>
									<img
										src={ILLUSION_HERO.prime}
										alt={t('ill.prime_label')}
										width={ILLUSION_HERO.primeWidth}
										height={ILLUSION_HERO.primeHeight}
										loading="lazy"
										decoding="async"
									/>
									<figcaption>{t('ill.prime_label')}</figcaption>
								</figure>
								<figure>
									<img
										src={ILLUSION_HERO.view}
										alt={t('ill.worked_view')}
										width={ILLUSION_HERO.viewWidth}
										height={ILLUSION_HERO.viewHeight}
										loading="lazy"
										decoding="async"
									/>
									<figcaption>{t('ill.worked_view')}</figcaption>
								</figure>
								<figure>
									<img
										class="turned"
										src={ILLUSION_HERO.view}
										alt={t('ill.worked_turned')}
										width={ILLUSION_HERO.viewWidth}
										height={ILLUSION_HERO.viewHeight}
										loading="lazy"
										decoding="async"
									/>
									<figcaption>{t('ill.worked_turned')}</figcaption>
								</figure>
							</div>
						{/if}
						{#if 'figures' in section}
							{#each section.figures as figure (figure.src)}
								<div class="fig-scroll">
									<figure>
										<img
											src={figure.src}
											alt={t(figure.cap)}
											width={figure.width}
											height={figure.height}
											loading="lazy"
										/>
										<figcaption>{t(figure.cap)}</figcaption>
									</figure>
								</div>
							{/each}
						{/if}
					</section>
				{/each}

				<p class="attr">{t('ill.attr')}</p>
				<div class="actions closing-actions">
					<a class="pill pill-accent" href={resolve('/whitepaper')}>{t('ill.cta_whitepaper')}</a>
					<a class="pill pill-ghost" href={ILLUSION_PAPER_URL}>{t('ill.cta_paper')}</a>
					<a class="pill pill-ghost" href={repoUrl}>{t('ill.cta_github')}</a>
				</div>
			</article>
		</div>
	</main>
</LatentShell>

<ScrollToTop />

<style>
	/* Hallmark - macrostructure: Latent Document - genre: abstract atmospheric - whitepaper shell with a flip stage in the opening - contrast: pass - mobile: pass */
	main {
		position: relative;
		z-index: 1;
		display: grid;
	}

	.opening {
		display: grid;
		justify-items: start;
		gap: 1.1rem;
		max-width: 52rem;
		padding: clamp(3rem, 9vw, 6rem) clamp(1rem, 5vw, 4rem) clamp(2rem, 6vw, 4rem);
	}

	h1 {
		font-size: clamp(2.4rem, 5.5vw, 4.4rem);
		line-height: 0.98;
		overflow-wrap: anywhere;
	}

	.lede,
	.hint {
		max-width: 52ch;
		color: var(--k-muted);
	}

	.lede {
		font-size: 1.05rem;
	}

	.hint {
		font-size: 0.88rem;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
	}

	.panel {
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		backdrop-filter: blur(28px);
	}

	:global(:root[data-landing-mode='light']) .panel {
		background: oklch(0.97 0.004 255 / 78%);
	}

	.document {
		display: grid;
		gap: clamp(2rem, 5vw, 4rem);
		padding-block: clamp(3rem, 8vw, 5rem);
		padding-inline: max(clamp(1rem, 4vw, 3rem), calc((100% - 72rem) / 2));
	}

	aside {
		display: none;
		position: sticky;
		inset-block-start: 1.5rem;
		align-self: start;
	}

	.rail-label {
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	aside ol {
		display: grid;
		margin: 0.85rem 0 0;
		padding: 0;
		list-style: none;
		border-inline-start: 1px solid var(--k-line);
	}

	aside a {
		display: block;
		margin-inline-start: -1px;
		padding: 0.4rem 0 0.4rem 0.9rem;
		border-inline-start: 2px solid transparent;
		color: var(--k-muted);
		font-size: 0.88rem;
		white-space: normal;
		transition: color 140ms var(--k-ease);
	}

	aside a:hover {
		border-inline-start-color: var(--k-accent);
		color: var(--k-ink);
	}

	article {
		display: grid;
		gap: clamp(2.5rem, 5vw, 3.5rem);
		min-width: 0;
	}

	article section {
		display: grid;
		gap: 0.9rem;
		scroll-margin-block-start: 2rem;
	}

	h2 {
		font-size: clamp(1.5rem, 2.6vw, 2rem);
		line-height: 1.1;
	}

	article p {
		max-width: 68ch;
		color: var(--k-muted);
		line-height: 1.75;
	}

	.gallery {
		display: grid;
		gap: 1.25rem;
		grid-template-columns: repeat(2, minmax(0, 1fr));
	}

	.worked {
		display: grid;
		gap: 1rem;
		grid-template-columns: repeat(auto-fit, minmax(min(100%, 11rem), 1fr));
	}

	.worked img.turned {
		transform: rotate(180deg);
	}

	.fig-scroll {
		overflow-x: auto;
		max-width: 100%;
	}

	figure {
		margin: 0.5rem 0 0;
		padding: 0.75rem;
		border: 1px solid var(--k-line);
		border-radius: 1rem;
		background: oklch(0.99 0.003 255);
	}

	figure img {
		display: block;
		width: 100%;
		height: auto;
		border-radius: 0.6rem;
	}

	figcaption {
		padding: 0.6rem 0.25rem 0.1rem;
		color: oklch(0.42 0.02 258);
		font-size: 0.78rem;
		line-height: 1.5;
	}

	.attr {
		max-width: 68ch;
		color: var(--k-muted);
		font-size: 0.82rem;
	}

	.closing-actions {
		padding-block-start: 0.5rem;
	}

	@media (min-width: 64rem) {
		.document {
			grid-template-columns: 14rem minmax(0, 1fr);
		}

		.gallery {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}

		aside {
			display: block;
		}
	}
</style>
