<script lang="ts">
	import { resolve } from '$app/paths';
	import ShieldCheckIcon from '@lucide/svelte/icons/shield-check';
	import ArrowUpIcon from '@lucide/svelte/icons/arrow-up';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import ParticleSea from './ParticleSea.svelte';
	import { t } from '$lib/i18n.svelte';

	let {
		/** Dark footer whose wordmark sets into a particle sea under a blue
		 * sunrise. */
		horizon = false
	}: { horizon?: boolean } = $props();

	let wordmark: HTMLElement | undefined = $state();

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
			<button type="button" class="to-top" onclick={toTop}>
				{t('nav.scroll_to_top')}
				<ArrowUpIcon aria-hidden="true" />
			</button>
		{/if}
	</div>
{/snippet}

<footer class:horizon>
	<div class="top">
		<div class="brand">
			<a class="mark" href={resolve('/')}><BrandMark /></a>
			<p>{t('footer.tagline')}</p>
			{#if horizon}
				<div class="ctas">
					<a class="cta cta-solid" href={resolve('/app')}>{t('nav.launch')}</a>
					<a class="cta cta-ghost" href={repoUrl}>{t('hero.cta_selfhost')}</a>
				</div>
			{/if}
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
			<ParticleSea variant="footer" horizonAt={wordmark} />
			<p class="wordmark" aria-hidden="true" bind:this={wordmark}>potocolom</p>
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
		padding: 3rem clamp(1rem, 4vw, 3rem) calc(2rem + env(safe-area-inset-bottom));
		border-block-start: 1px solid var(--k-line);
		background: oklch(0.08 0.012 265 / 72%);
		color: var(--k-muted);
		font-size: 0.875rem;
		backdrop-filter: blur(28px);
	}

	:global(:root[data-landing-mode='light']) footer {
		background: oklch(0.97 0.004 255 / 78%);
	}

	/* The sea's glow rises behind the columns, so they sit above it. */
	.top,
	.trust {
		position: relative;
		z-index: 1;
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

	/* The horizon footer is the dark page in both landing modes, so its text
	   tokens are set for light ink throughout. */
	footer.horizon {
		--k-ink: #f7f8fa;
		--k-muted: #a6abb5;
		--k-line: rgb(255 255 255 / 12%);
		overflow: hidden;
		padding-block-end: 0;
		border-block-start: 0;
		background: #07090f;
		backdrop-filter: none;
	}

	:global(:root[data-landing-mode='light']) footer.horizon {
		background: #07090f;
	}

	.ctas {
		display: flex;
		flex-wrap: wrap;
		gap: 0.625rem;
		margin-block-start: 1rem;
	}

	.cta {
		display: inline-flex;
		align-items: center;
		min-block-size: 2.75rem;
		padding-inline: 1.25rem;
		border: 1px solid transparent;
		border-radius: 999px;
		color: #f7f8fa;
		font-weight: 700;
	}

	.cta-solid {
		background: #3a64f0;
		color: #ffffff;
	}

	.cta-solid:hover {
		background: #3357d6;
		color: #ffffff;
	}

	.cta-ghost {
		border-color: rgb(255 255 255 / 18%);
	}

	.cta-ghost:hover {
		border-color: rgb(255 255 255 / 36%);
	}

	.cta:focus-visible,
	footer.horizon a:focus-visible {
		outline: 2px solid #6f97ff;
		outline-offset: 3px;
	}

	/* The band holds the sea: the horizon sits just above the wordmark, which
	   is set in the page colour so it reads as a silhouette against the glow
	   and the water. The glow and fade are CSS, so without a canvas the band is
	   still a sunrise over a dark page. The wordmark and the copyright stack at
	   its foot, and the page's own floating controls sit in the bottom corners. */
	.band {
		position: relative;
		display: flex;
		flex-direction: column;
		justify-content: flex-end;
		block-size: clamp(26rem, 42vw, 36rem);
		margin-inline: calc(clamp(1rem, 4vw, 3rem) * -1);
		background-color: #07090f;
	}

	@media (max-width: 47.99rem) {
		.band {
			block-size: 24rem;
		}
	}

	.wordmark {
		/* The 0.2em clears the descender of the p; the rem is the gap above the
		   copyright line. */
		position: relative;
		z-index: 1;
		margin: 0 0 calc(0.2em + 1rem);
		color: #07090f;
		font-size: clamp(5rem, 18.6vw, 16.75rem);
		font-weight: 900;
		line-height: 0.8;
		letter-spacing: -0.07em;
		text-align: center;
		white-space: nowrap;
		opacity: 0.92;
		pointer-events: none;
	}

	/* The band bleeds to the footer's edges, so its copyright line takes the
	   footer's gutter back to line up with the columns above. */
	.band .bottom {
		position: relative;
		z-index: 1;
		box-sizing: border-box;
		max-width: calc(78rem + 2 * clamp(1rem, 4vw, 3rem));
		align-items: center;
		/* The floating language and theme pill sits over this row; both clear
		   the home indicator. */
		padding-block-end: calc(4.5rem + env(safe-area-inset-bottom));
		padding-inline: clamp(1rem, 4vw, 3rem);
		color: #a6abb5;
		font-size: 0.75rem;
		font-weight: 700;
		line-height: 1rem;
	}

	.to-top {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		min-block-size: 2.75rem;
		padding: 0;
		border: 0;
		background: none;
		color: inherit;
		font: inherit;
		text-decoration: underline;
		text-underline-offset: 0.2em;
		cursor: pointer;
	}

	.to-top:hover {
		color: #f7f8fa;
	}

	.to-top :global(svg) {
		inline-size: 0.875rem;
		block-size: 0.875rem;
	}

	.to-top:focus-visible {
		outline: 2px solid currentColor;
		outline-offset: 3px;
	}

	@media (min-width: 48rem) {
		.top {
			grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
		}
	}
</style>
