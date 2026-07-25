<script lang="ts">
	import { resolve } from '$app/paths';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	// ponytail: captions are each image's own description; the marquee prompts belong
	// to different images, so pairing them here would be a lie.
	const panels = collageImages.slice(0, 14);
</script>

<div class="krea river">
	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<p class="hint">{t('sketch.sideways')}</p>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<div class="track">
		<section class="panel opener">
			<h1>{t('hero.title1')} {t('hero.title2')}</h1>
			<p>{t('hero.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
			</div>
		</section>

		{#each panels as tile, position (tile.file)}
			{@const sources = collageLandingSources(tile)}
			<figure class="panel frame" class:tall={position % 3 === 1}>
				<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
				<figcaption>{tile.alt}</figcaption>
			</figure>
		{/each}

		<section class="panel closer">
			<h2>{t('wl.title')}</h2>
			<p>{t('wl.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('wl.cta')}</a>
				<a class="pill pill-ghost" href={repoUrl}>{t('fork.cta_source')}</a>
			</div>
			<p class="fine">{t('footer.tagline')}</p>
		</section>
	</div>
</div>

<style>
	/* Hallmark - macrostructure: Horizontal River - genre: contact sheet in motion - enrichment: the page travels sideways through real work - contrast: pass - mobile: pass (falls back to vertical) */
	.river {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		grid-template-rows: auto 1fr;
		height: 100svh;
		overflow: clip;
	}

	header {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
		border-block-end: 1px solid var(--k-line);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	.hint {
		display: none;
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.72rem;
		text-align: center;
	}

	.track {
		display: flex;
		min-width: 0;
		gap: clamp(0.75rem, 2vw, 1.5rem);
		min-height: 0;
		overflow-x: auto;
		overflow-y: clip;
		padding: clamp(1rem, 3vw, 2rem);
		scroll-snap-type: x proximity;
	}

	.panel {
		flex: none;
		min-width: 0;
		height: 100%;
		margin: 0;
		scroll-snap-align: center;
	}

	.opener,
	.closer {
		display: grid;
		align-content: center;
		gap: 1.1rem;
		width: min(34rem, 80vw);
		padding-inline-end: clamp(1rem, 3vw, 2rem);
	}

	h1 {
		font-size: clamp(2.6rem, 5.5vw, 4.6rem);
		line-height: 0.96;
	}

	h2 {
		font-size: clamp(2rem, 4vw, 3.2rem);
		line-height: 1;
	}

	.panel p {
		max-width: 40ch;
		color: var(--k-muted);
	}

	.fine {
		font-size: 0.82rem;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
	}

	.frame {
		display: grid;
		grid-template-rows: minmax(0, 1fr) auto;
		gap: 0.6rem;
		align-self: center;
		height: min(78%, 34rem);
	}

	.frame.tall {
		height: 100%;
	}

	.frame img {
		width: auto;
		height: 100%;
		max-width: 80vw;
		border-radius: 0.75rem;
		object-fit: cover;
	}

	figcaption {
		max-width: 28ch;
		color: var(--k-muted);
		font-family: var(--k-mono);
		font-size: 0.7rem;
		line-height: 1.5;
	}

	@media (min-width: 48rem) {
		.hint {
			display: block;
		}
	}

	@media (max-width: 48rem) {
		.river {
			height: auto;
			min-height: 100svh;
		}

		.track {
			flex-direction: column;
			overflow-x: clip;
			overflow-y: visible;
			scroll-snap-type: none;
		}

		.panel {
			width: auto;
			height: auto;
		}

		.opener,
		.closer {
			width: auto;
			min-height: 70svh;
		}

		.frame,
		.frame.tall {
			height: auto;
		}

		.frame img {
			width: 100%;
			max-width: none;
			height: auto;
			aspect-ratio: 4 / 3;
		}
	}
</style>
