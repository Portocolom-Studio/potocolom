<script lang="ts">
	import { resolve } from '$app/paths';
	import ShieldCheckIcon from '@lucide/svelte/icons/shield-check';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import { t } from '$lib/i18n.svelte';

	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const year = new Date().getFullYear();
</script>

<footer>
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

	<div class="bottom">
		<span>{t('footer.copyright').replace('{year}', String(year))}</span>
	</div>
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

	@media (min-width: 48rem) {
		.top {
			grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
		}
	}
</style>
