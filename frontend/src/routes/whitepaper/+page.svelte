<script lang="ts">
	import { resolve } from '$app/paths';
	import LatentShell from '$lib/components/landing-krea/LatentShell.svelte';
	import Seo from '$lib/components/Seo.svelte';
	import ScrollToTop from '$lib/components/ScrollToTop.svelte';
	import { t } from '$lib/i18n.svelte';
	import '../../krea-tokens.css';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	const sections = [
		{ id: 's1', title: 'wp.s1_title', paragraphs: ['wp.s1_p1', 'wp.s1_p2'] },
		{
			id: 's2',
			title: 'wp.s2_title',
			paragraphs: ['wp.s2_p1', 'wp.s2_p2', 'wp.s2_p3'],
			figure: {
				src: '/whitepaper/under-the-hood.webp',
				cap: 'wp.fig_arch_cap',
				width: 3146,
				height: 1084
			}
		},
		{
			id: 's3',
			title: 'wp.s3_title',
			paragraphs: ['wp.s3_p1', 'wp.s3_p2', 'wp.s3_p3'],
			figure: {
				src: '/whitepaper/realtime-loop.webp',
				cap: 'wp.fig_loop_cap',
				width: 2365,
				height: 1587
			}
		},
		{ id: 's4', title: 'wp.s4_title', paragraphs: ['wp.s4_p1', 'wp.s4_p2', 'wp.s4_p3'] },
		{ id: 's5', title: 'wp.s5_title', paragraphs: ['wp.s5_p1', 'wp.s5_p2', 'wp.s5_p3'] },
		{ id: 's6', title: 'wp.s6_title', paragraphs: ['wp.s6_p1', 'wp.s6_p2', 'wp.s6_p3'] },
		{
			id: 's7',
			title: 'wp.s7_title',
			paragraphs: ['wp.s7_p1', 'wp.s7_p2', 'wp.s7_p3'],
			figure: {
				src: '/whitepaper/credit-lifecycle.webp',
				cap: 'wp.fig_credits_cap',
				width: 2325,
				height: 1627
			}
		},
		{
			id: 's8',
			title: 'wp.s8_title',
			paragraphs: ['wp.s8_p1', 'wp.s8_p2'],
			figure: {
				src: '/whitepaper/failure-map.webp',
				cap: 'wp.fig_failures_cap',
				width: 2363,
				height: 1502
			}
		},
		{ id: 's9', title: 'wp.s9_title', paragraphs: ['wp.s9_p1', 'wp.s9_p2'] }
	] as const;
</script>

<Seo
	title="potocolom Architecture Whitepaper | Realtime AI Images"
	description="Read how potocolom designs realtime canvas generation, GPU scheduling, self-hosting, privacy, and a shared AGPL-3.0 codebase."
	path="/whitepaper"
/>

<LatentShell current="whitepaper">
	<main>
		<section class="opening">
			<h1>{t('wp.title')}</h1>
			<p class="lede">{t('wp.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href="{repoUrl}/tree/main/docs">{t('wp.cta_docs')}</a>
				<a class="pill pill-ghost" href={resolve('/benchmark')}>{t('wp.cta_benchmark')}</a>
			</div>
		</section>

		<div class="panel document">
			<aside aria-label={t('wp.toc')}>
				<p class="rail-label">{t('wp.toc')}</p>
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
							<p>{t(paragraph)}</p>
						{/each}
						{#if 'figure' in section}
							<figure>
								<img
									src={section.figure.src}
									alt={t(section.figure.cap)}
									width={section.figure.width}
									height={section.figure.height}
									loading="lazy"
								/>
								<figcaption>{t(section.figure.cap)}</figcaption>
							</figure>
						{/if}
					</section>
				{/each}

				<div class="actions closing-actions">
					<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
					<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
				</div>
			</article>
		</div>
	</main>
</LatentShell>

<ScrollToTop />

<style>
	/* Hallmark - macrostructure: Latent Document - genre: abstract atmospheric - the landing's canvas and panels carry the whitepaper - contrast: pass - mobile: pass */
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
	}

	.lede {
		max-width: 52ch;
		color: var(--k-muted);
		font-size: 1.05rem;
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

	:global(:root[data-krea-mode='light']) .panel {
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

	/* The diagrams are dark-on-light, so they keep a paper card of their own. */
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
		border-radius: 0.6rem;
	}

	figcaption {
		padding: 0.6rem 0.25rem 0.1rem;
		color: oklch(0.42 0.02 258);
		font-size: 0.78rem;
		line-height: 1.5;
	}

	.closing-actions {
		padding-block-start: 0.5rem;
	}

	@media (min-width: 64rem) {
		.document {
			grid-template-columns: 14rem minmax(0, 1fr);
		}

		aside {
			display: block;
		}
	}
</style>
