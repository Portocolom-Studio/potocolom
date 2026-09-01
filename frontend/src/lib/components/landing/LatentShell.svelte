<script lang="ts">
	import { resolve } from '$app/paths';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';
	import { t } from '$lib/i18n.svelte';
	import type { Snippet } from 'svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';

	let {
		current,
		children
	}: {
		current?: 'whitepaper' | 'benchmark';
		children: Snippet;
	} = $props();
</script>

<div class="landing-surface latent-page">
	<div class="canvas" aria-hidden="true">
		<LatentCanvas followCursor animate warmupFrames={1400} />
	</div>
	<div class="veil" aria-hidden="true"></div>

	<header>
		<a class="mark" href={resolve('/')}>potocolom</a>
		<nav aria-label={t('nav.features')}>
			<a href={resolve('/whitepaper')} aria-current={current === 'whitepaper' ? 'page' : undefined}>
				{t('nav.whitepaper')}
			</a>
			<a href={resolve('/benchmark')} aria-current={current === 'benchmark' ? 'page' : undefined}>
				{t('nav.benchmark')}
			</a>
			<a href={repoUrl}>{t('nav.open')}</a>
		</nav>
		<div class="chrome-actions">
			<LanguageToggle />
			<a class="pill pill-ghost" href={resolve('/app')}>{t('nav.launch')}</a>
		</div>
	</header>

	{@render children()}

	<footer>
		<p>{t('footer.tagline')}</p>
		<nav aria-label={t('footer.docs')}>
			<a href={repoUrl}>{t('footer.github')}</a>
			<a href={`${repoUrl}/tree/main/docs`}>{t('footer.docs')}</a>
			<a href={resolve('/legal')}>{t('footer.legal')}</a>
			<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
			<a href="mailto:admin@leonfuller.com">{t('footer.contact')}</a>
		</nav>
	</footer>
</div>

<style>
	/* Hallmark - the latent chrome, reused by the document pages */
	.latent-page {
		position: relative;
		min-width: 0;
		min-height: 100svh;
		overflow-x: clip;
	}

	.canvas,
	.veil {
		position: fixed;
		inset: 0;
		z-index: 0;
	}

	.veil {
		background:
			radial-gradient(38% 46% at 26% 30%, oklch(0.62 0.2 255 / 18%) 0%, transparent 72%),
			radial-gradient(70% 60% at 40% 30%, transparent 0%, var(--k-veil) 88%);
		pointer-events: none;
	}

	header,
	footer {
		position: relative;
		z-index: 1;
	}

	header {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 1rem;
		padding: 1.1rem clamp(1rem, 3vw, 2.5rem);
	}

	.chrome-actions {
		display: flex;
		align-items: center;
		gap: 0.75rem;
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

	header nav a:hover,
	header nav a[aria-current='page'] {
		color: var(--k-ink);
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 1rem 2rem;
		padding: 2rem clamp(1rem, 4vw, 3rem);
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		color: var(--k-muted);
		font-size: 0.85rem;
		backdrop-filter: blur(28px);
	}

	:global(:root[data-landing-mode='light']) footer {
		background: oklch(0.97 0.004 255 / 78%);
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
	}
</style>
