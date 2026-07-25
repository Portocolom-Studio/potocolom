<script lang="ts">
	import { resolve } from '$app/paths';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const capabilities = ['live', 'gen', 'up', 'edit'] as const;
	const strip = collageImages.slice(0, 8);
</script>

<div class="krea desk">
	<section class="scene">
		<img class="shot" src="/og.png" alt={t('app.title')} />
		<div class="vignette" aria-hidden="true"></div>

		<header>
			<a class="mark" href={resolve('/')}>potocolom</a>
			<nav aria-label={t('nav.features')}>
				<a href="#does">{t('nav.features')}</a>
				<a href="#work">{t('gallery.kicker')}</a>
				<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
			</nav>
			<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
		</header>

		<div class="stage">
			<p class="badge">{t('hero.kicker')}</p>
			<h1>{t('hero.title1')} {t('hero.title2')}</h1>
			<p class="lede">{t('hero.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
			</div>
		</div>
	</section>

	<section id="does" class="does">
		{#each capabilities as capability (capability)}
			<article>
				<h2>{t(`caps.${capability}_title`)}</h2>
				<p>{t(`caps.${capability}_body`)}</p>
			</article>
		{/each}
	</section>

	<section id="work" class="work">
		<div class="work-strip">
			{#each strip as tile (tile.file)}
				{@const sources = collageLandingSources(tile)}
				<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
			{/each}
		</div>
		<p>{t('gallery.sub')}</p>
	</section>

	<footer>
		<p>{t('footer.tagline')}</p>
		<nav aria-label={t('footer.docs')}>
			<a href={repoUrl}>{t('footer.github')}</a>
			<a href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
			<a href={resolve('/legal')}>{t('footer.legal')}</a>
			<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
		</nav>
	</footer>
</div>

<style>
	/* Hallmark - macrostructure: Product In Scene - genre: cinematic - enrichment: the real studio screenshot as the whole first screen - contrast: pass - mobile: pass */
	.desk {
		min-width: 0;
		overflow-x: clip;
	}

	.scene {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		grid-template-rows: auto 1fr;
		height: 100svh;
		overflow: clip;
	}

	.shot,
	.vignette {
		position: absolute;
		inset: 0;
	}

	.shot {
		width: 100%;
		height: 100%;
		object-fit: cover;
		object-position: 65% 40%;
		filter: brightness(0.72) saturate(0.92);
	}

	.vignette {
		background:
			linear-gradient(
				to right,
				var(--k-paper) 4%,
				oklch(0.08 0.012 265 / 72%) 46%,
				transparent 82%
			),
			linear-gradient(to bottom, var(--k-veil) 0%, transparent 30%, var(--k-paper) 97%);
	}

	header,
	.stage {
		position: relative;
		z-index: 2;
	}

	header {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	header nav {
		display: none;
		justify-content: center;
		gap: 1.75rem;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.stage {
		display: grid;
		align-content: center;
		justify-items: start;
		gap: 1.1rem;
		max-width: 40rem;
		padding: 0 clamp(1rem, 4vw, 3rem) clamp(2rem, 8vh, 6rem);
	}

	.badge {
		padding: 0.35rem 0.85rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.68rem;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		backdrop-filter: blur(12px);
	}

	h1 {
		max-width: 18ch;
		font-size: clamp(2.6rem, 6vw, 5.4rem);
		line-height: 0.98;
	}

	.lede {
		max-width: 46ch;
		color: var(--k-muted);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
	}

	.does {
		display: grid;
		gap: 2rem;
		max-width: 82rem;
		margin-inline: auto;
		padding: clamp(3rem, 7vw, 5rem) clamp(1rem, 4vw, 2.5rem);
	}

	.does article {
		display: grid;
		gap: 0.5rem;
		padding-block-start: 1rem;
		border-block-start: 1px solid var(--k-line);
	}

	.does h2 {
		font-size: 1.25rem;
	}

	.does p {
		max-width: 34ch;
		color: var(--k-muted);
		font-size: 0.92rem;
	}

	.work {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 1.25rem;
		padding-block-end: clamp(3rem, 7vw, 5rem);
	}

	.work-strip {
		display: flex;
		min-width: 0;
		gap: 0.5rem;
		overflow-x: auto;
		padding-inline: clamp(1rem, 4vw, 2.5rem);
		scroll-snap-type: x mandatory;
	}

	.work-strip img {
		width: clamp(9rem, 22vw, 16rem);
		height: clamp(9rem, 22vw, 16rem);
		flex: none;
		border-radius: 1rem;
		object-fit: cover;
		scroll-snap-align: center;
	}

	.work p {
		max-width: 52ch;
		margin-inline: auto;
		padding-inline: clamp(1rem, 4vw, 2.5rem);
		color: var(--k-muted);
		font-size: 0.9rem;
		text-align: center;
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem 2rem;
		max-width: 82rem;
		margin-inline: auto;
		padding: 2rem clamp(1rem, 4vw, 2.5rem);
		border-block-start: 1px solid var(--k-line);
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	footer nav {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem 1.25rem;
	}

	@media (min-width: 48rem) {
		header nav {
			display: flex;
		}

		.does {
			grid-template-columns: repeat(4, minmax(0, 1fr));
		}
	}

	@media (max-width: 30rem) {
		.actions {
			width: 100%;
			flex-direction: column;
		}

		.actions .pill {
			width: 100%;
		}
	}
</style>
