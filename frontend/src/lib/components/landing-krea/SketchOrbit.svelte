<script lang="ts">
	import { resolve } from '$app/paths';
	import HeroImageField from '$lib/components/HeroImageField.svelte';
	import { collageImages, collageLandingSources } from '$lib/collage-images';
	import { promptMarqueePrompts } from '$lib/prompt-marquee-prompts';
	import { t } from '$lib/i18n.svelte';
	import { onMount } from 'svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const arcTiles = collageImages.slice(0, 14);

	// The headline types real sample prompts, one character at a time.
	let typed = $state('');
	let promptIndex = $state(0);

	onMount(() => {
		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		if (reduced) {
			typed = promptMarqueePrompts[0].primary;
			return;
		}

		let charIndex = 0;
		let holding = 0;
		const timer = setInterval(() => {
			const full = promptMarqueePrompts[promptIndex].primary;
			if (charIndex < full.length) {
				charIndex += 1;
				typed = full.slice(0, charIndex);
				return;
			}
			holding += 1;
			if (holding < 26) return;
			holding = 0;
			charIndex = 0;
			typed = '';
			promptIndex = (promptIndex + 1) % promptMarqueePrompts.length;
		}, 55);
		return () => clearInterval(timer);
	});
</script>

<div class="krea orbit">
	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
	</header>

	<section class="empty-stage">
		<h1>
			<span class="line">{t('hero.title1')}</span>
			<span class="line typing">
				<span class="typed">{typed}</span><span class="caret" aria-hidden="true"></span>
			</span>
		</h1>
		<p class="lede">{t('hero.sub')}</p>
		<div class="actions">
			<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
			<a class="pill pill-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
		</div>

		<div class="arc" aria-label={t('gallery.kicker')}>
			<div class="arc-spin">
				{#each arcTiles as tile, index (tile.file)}
					{@const sources = collageLandingSources(tile)}
					<span class="chip" style="--i: {index}; --n: {arcTiles.length}">
						<img src={sources.src} srcset={sources.srcset} alt={tile.alt} loading="lazy" />
					</span>
				{/each}
			</div>
		</div>
	</section>

	<section class="reveal">
		<div class="card"><HeroImageField /></div>
		<div class="reveal-copy">
			<h2>{t('caps.title')}</h2>
			<p>{t('caps.sub')}</p>
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
			</div>
		</div>
	</section>

	<footer>
		<p>{t('footer.tagline')}</p>
		<nav aria-label={t('footer.docs')}>
			<a href={repoUrl}>{t('footer.github')}</a>
			<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
			<a href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
			<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
		</nav>
	</footer>
</div>

<style>
	/* Hallmark - macrostructure: Empty Stage - genre: spare, one motion only - studied DNA: antigravity.google - enrichment: self-typing real prompts over an orbiting ring of real work - contrast: pass - mobile: pass */
	.orbit {
		min-width: 0;
		overflow-x: clip;
	}

	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
		padding: 1.25rem clamp(1rem, 4vw, 3rem);
	}

	.mark {
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	/* Stage ---------------------------------------------------------------- */
	.empty-stage {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		justify-items: center;
		align-content: center;
		gap: 1.5rem;
		min-height: calc(100svh - 6rem);
		padding: clamp(2rem, 8vh, 6rem) clamp(1rem, 4vw, 3rem) 0;
		text-align: center;
	}

	h1 {
		display: grid;
		gap: 0.2rem;
		max-width: 22ch;
		font-size: clamp(2.4rem, 6vw, 5rem);
		line-height: 1.02;
	}

	.line {
		display: block;
		min-width: 0;
	}

	.typing {
		color: var(--k-muted);
		font-weight: 500;
		min-height: 1.1em;
	}

	.typed {
		overflow-wrap: anywhere;
	}

	.caret {
		display: inline-block;
		width: 0.06em;
		height: 0.9em;
		margin-inline-start: 0.06em;
		background: var(--k-accent);
		vertical-align: -0.08em;
		animation: blink 1.1s steps(1, end) infinite;
	}

	@keyframes blink {
		50% {
			opacity: 0;
		}
	}

	.lede {
		max-width: 46ch;
		color: var(--k-muted);
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
	}

	/* The ring: chips sit on a circle far below the fold and drift around it. */
	.arc {
		position: relative;
		width: 100%;
		height: clamp(8rem, 20vh, 12rem);
		margin-block-start: auto;
		overflow: clip;
	}

	/* Circle centre sits far below the band, so the chips ride its top arc. */
	.arc-spin {
		position: absolute;
		inset-block-start: 80rem;
		inset-inline-start: 50%;
		width: 0;
		height: 0;
		animation: sway 44s ease-in-out infinite alternate;
	}

	@keyframes sway {
		from {
			transform: rotate(-3.5deg);
		}
		to {
			transform: rotate(3.5deg);
		}
	}

	.chip {
		position: absolute;
		display: block;
		width: clamp(3.2rem, 6vw, 4.6rem);
		aspect-ratio: 1;
		margin: -0.5rem 0 0 -0.5rem;
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		transform: rotate(calc((var(--i) - (var(--n) - 1) / 2) * 4deg)) translateY(-80rem);
		transition: transform 300ms var(--k-ease);
	}

	.chip img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	/* Reveal --------------------------------------------------------------- */
	.reveal {
		display: grid;
		gap: clamp(1.5rem, 4vw, 3rem);
		max-width: 78rem;
		margin-inline: auto;
		padding: clamp(3rem, 9vw, 7rem) clamp(1rem, 4vw, 3rem);
	}

	.card {
		aspect-ratio: 16 / 10;
		min-height: 16rem;
		overflow: clip;
		border: 1px solid var(--k-line);
		border-radius: 1.75rem;
		background: var(--k-screen);
	}

	.reveal-copy {
		display: grid;
		justify-items: center;
		gap: 1rem;
		max-width: 46rem;
		margin-inline: auto;
		text-align: center;
	}

	h2 {
		font-size: clamp(1.9rem, 3.6vw, 3rem);
		line-height: 1.04;
	}

	.reveal-copy p {
		color: var(--k-muted);
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem 2rem;
		max-width: 78rem;
		margin-inline: auto;
		padding: 2rem clamp(1rem, 4vw, 3rem);
		border-block-start: 1px solid var(--k-line);
		color: var(--k-muted);
		font-size: 0.85rem;
	}

	footer nav {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem 1.25rem;
	}

	@media (hover: hover) and (pointer: fine) {
		.chip:hover {
			transform: rotate(calc((var(--i) - (var(--n) - 1) / 2) * 4deg)) translateY(-80.7rem)
				scale(1.22);
		}
	}

	@media (max-width: 40rem) {
		.chip {
			transform: rotate(calc((var(--i) - (var(--n) - 1) / 2) * 7deg)) translateY(-80rem);
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

	@media (prefers-reduced-motion: reduce) {
		.arc-spin,
		.caret {
			animation: none;
		}
	}
</style>
