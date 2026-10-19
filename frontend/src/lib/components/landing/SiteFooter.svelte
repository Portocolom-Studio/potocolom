<script lang="ts">
	import { resolve } from '$app/paths';
	import ShieldCheckIcon from '@lucide/svelte/icons/shield-check';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import { t } from '$lib/i18n.svelte';

	let {
		/** Solid navy footer that ends in the sunrise band behind the wordmark. */
		horizon = false
	}: { horizon?: boolean } = $props();

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const year = new Date().getFullYear();

	function toTop() {
		window.scrollTo({
			top: 0,
			behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
		});
	}
</script>

{#snippet bottomLine()}
	<div class="bottom">
		<span>{t('footer.copyright').replace('{year}', String(year))}</span>
		{#if horizon}
			<button type="button" class="to-top" onclick={toTop}>{t('nav.scroll_to_top')}</button>
		{/if}
	</div>
{/snippet}

<footer class:horizon>
	<div class="top">
		<div class="brand">
			<a class="mark" href={resolve('/')}><BrandMark /></a>
			<p>{t('footer.tagline')}</p>
		</div>

		<nav class="columns" aria-label={t('nav.footer')}>
			<div>
				<h2>{t('footer.col_product')}</h2>
				<a href={resolve('/app')}>{t('nav.launch')}</a>
				<a href={resolve('/whitepaper')}>{t('nav.whitepaper')}</a>
				<a href={resolve('/benchmark')}>{t('nav.benchmark')}</a>
				<a href={resolve('/illusions')}>{t('nav.illusions')}</a>
			</div>
			<div>
				<h2>{t('footer.col_source')}</h2>
				<a href={repoUrl}>{t('footer.github')}</a>
				<a href={`${repoUrl}/tree/main/docs`}>{t('footer.docs')}</a>
				<a href={`${repoUrl}/blob/main/LICENSE`}>{t('footer.license')}</a>
			</div>
			<div>
				<h2>{t('footer.col_company')}</h2>
				<a href="mailto:admin@leonfuller.com">{t('footer.contact')}</a>
				<a href={resolve('/legal')}>{t('footer.legal')}</a>
				<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
			</div>
		</nav>
	</div>

	<section class="trust" aria-label={t('footer.col_trust')}>
		<div class="badge">
			<ShieldCheckIcon aria-hidden="true" />
			<span>{t('footer.gdpr_badge')}</span>
		</div>
		<p>
			{t('footer.gdpr_body')}
			<a href={resolve('/privacy')}>{t('footer.privacy')}</a>
		</p>
	</section>

	{#if horizon}
		<div class="band">
			<div class="sky" aria-hidden="true"></div>
			<div class="band-veil" aria-hidden="true"></div>
			<p class="wordmark" aria-hidden="true">potocolom</p>
			{@render bottomLine()}
		</div>
	{:else}
		{@render bottomLine()}
	{/if}
</footer>

<style>
	footer {
		position: relative;
		z-index: 1;
		display: grid;
		gap: 2.25rem;
		padding: 3rem clamp(1rem, 4vw, 3rem) 2rem;
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		color: var(--k-muted);
		font-size: 0.875rem;
		backdrop-filter: blur(28px);
	}

	:global(:root[data-landing-mode='light']) footer {
		background: oklch(0.97 0.004 255 / 78%);
	}

	.top {
		display: grid;
		gap: 2rem;
		width: 100%;
		max-width: 78rem;
		margin-inline: auto;
	}

	.brand {
		display: grid;
		gap: 0.6rem;
		align-content: start;
	}

	.mark {
		color: var(--k-ink);
		font-size: 1.05rem;
		font-weight: 800;
		letter-spacing: -0.03em;
	}

	.brand p {
		max-width: 28ch;
		line-height: 1.55;
	}

	.columns {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
		gap: 1.5rem 2rem;
	}

	.columns div {
		display: grid;
		gap: 0.55rem;
		align-content: start;
	}

	h2 {
		margin-block-end: 0.2rem;
		color: var(--k-ink);
		font-size: 0.8rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}

	a:hover {
		color: var(--k-ink);
	}

	.trust {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem 1.25rem;
		width: 100%;
		max-width: 78rem;
		margin-inline: auto;
		padding-block-start: 1.5rem;
		border-block-start: 1px solid var(--k-line);
	}

	.badge {
		display: inline-flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.35rem 0.75rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		color: var(--k-ink);
		font-size: 0.8rem;
		font-weight: 700;
		letter-spacing: 0.04em;
	}

	.badge :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	.trust p {
		flex: 1 1 22rem;
		line-height: 1.55;
	}

	.trust p a {
		text-decoration: underline;
		text-underline-offset: 0.2em;
	}

	.bottom {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		gap: 0.5rem 1.5rem;
		width: 100%;
		max-width: 78rem;
		margin-inline: auto;
		font-size: 0.8rem;
	}

	/* The horizon footer is navy in both landing modes, so its text tokens are
	   set for dark ink throughout. */
	footer.horizon {
		--k-ink: #f4f6fb;
		--k-muted: #9aa4bd;
		--k-line: #1c2440;
		padding-block-end: 0;
		background: #0a1022;
	}

	:global(:root[data-landing-mode='light']) footer.horizon {
		background: #0a1022;
	}

	/* The wordmark and the copyright stack at the foot of the band rather than
	   overlapping: dark ink on dark letters is unreadable, and the page's own
	   floating controls sit in the bottom corners. */
	.band {
		position: relative;
		display: flex;
		flex-direction: column;
		justify-content: flex-end;
		overflow: hidden;
		block-size: clamp(16rem, 34vw, 26rem);
		margin-inline: calc(clamp(1rem, 4vw, 3rem) * -1);
	}

	.sky {
		position: absolute;
		inset: 0;
		background: url('/images/horizon/sunrise-ascii.webp') center bottom / cover no-repeat;
		filter: hue-rotate(195deg) saturate(1.1);
	}

	.band-veil {
		position: absolute;
		inset: 0;
		background: linear-gradient(180deg, #0a1022 0%, rgb(10 16 34 / 0%) 55%);
	}

	/* The short phone band would bury the wordmark in the veil, so the veil
	   gives way to the glow sooner there. */
	@media (max-width: 47.99rem) {
		.band {
			block-size: 22rem;
		}

		.band-veil {
			background: linear-gradient(180deg, #0a1022 0%, rgb(10 16 34 / 0%) 25%);
		}

		/* At phone width the copyright sits above the bright part of the glow,
		   so it takes the light ink of the rest of the footer. */
		.band .bottom {
			color: #f4f6fb;
		}
	}

	.wordmark {
		/* The 0.2em clears the descender of the p; the rem is the gap above the
		   copyright line. */
		position: relative;
		z-index: 1;
		margin: 0 0 calc(0.2em + 1rem);
		color: rgb(7 11 22 / 90%);
		font-size: clamp(5rem, 18vw, 16.5rem);
		font-weight: 700;
		line-height: 0.8;
		letter-spacing: -0.065em;
		text-align: center;
		white-space: nowrap;
		pointer-events: none;
	}

	.band .bottom {
		position: relative;
		z-index: 1;
		box-sizing: border-box;
		padding-block-end: 4.5rem;
		padding-inline: clamp(1rem, 4vw, 3rem);
		color: #070b16;
		font-size: 0.75rem;
		font-weight: 500;
		line-height: 1rem;
	}

	.to-top {
		padding: 0;
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		text-decoration: underline;
		text-underline-offset: 0.2em;
		cursor: pointer;
	}

	.to-top:focus-visible {
		outline: 2px solid #070b16;
		outline-offset: 3px;
	}

	@media (min-width: 48rem) {
		.top {
			grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
		}
	}
</style>
