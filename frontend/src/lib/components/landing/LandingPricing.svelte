<script lang="ts">
	import { resolve } from '$app/paths';
	import { getLocale, t } from '$lib/i18n.svelte';
	import { plans, planForHours } from '$lib/pricing-plans';
	import CheckIcon from '@lucide/svelte/icons/check';
	import ChevronDownIcon from '@lucide/svelte/icons/chevron-down';

	let { full = false }: { full?: boolean } = $props();

	const EURO = '\u20ac';
	const ossBullets = ['split.oss_p1', 'split.oss_p2', 'split.oss_p3'] as const;
	const bullets = ['b1', 'b2', 'b3'] as const;
	const ticks = [0, 5, 10, 15, 20, 25, 30];
	const billingQuestions = [
		{ question: 'faq.q5', answer: 'faq.a5' },
		{ question: 'faq.q2', answer: 'faq.a2' },
		{ question: 'pricing.q_prices', answer: 'pricing.a_prices' }
	] as const;
	const studioHours = plans.find((plan) => plan.key === 't3')?.hours ?? 0;

	let hours = $state(6);

	function formatNumber(value: number, maximumFractionDigits = 1): string {
		return value.toLocaleString(getLocale(), { maximumFractionDigits });
	}

	const hoursText = $derived(
		t(
			new Intl.PluralRules(getLocale()).select(hours) === 'one'
				? 'pricing.hours_one'
				: 'pricing.hours_other'
		).replace('{hours}', formatNumber(hours))
	);

	// The sentence for the plan that covers the slider value. Null means the
	// value is above the Studio hours, so the result points to a conversation.
	const estimate = $derived.by((): string | null => {
		const plan = plans.find((candidate) => candidate.key === planForHours(hours));
		if (!plan || plan.price === null || plan.credits === null || plan.hours === null) {
			return null;
		}
		return t('pricing.estimate_plan')
			.replace('{plan}', t(`pricing.${plan.key}_name`))
			.replace('{credits}', formatNumber(plan.credits, 0))
			.replace('{hours}', formatNumber(plan.hours))
			.replace('{price}', `${EURO}${plan.price}`)
			.replace('{month}', t('pricing.month'));
	});
</script>

<section id="pricing" class="pricing" aria-labelledby="pricing-title">
	<div class="head">
		<h2 id="pricing-title">{t('pricing.title')}</h2>
		<p>{t('pricing.sub')}</p>
	</div>

	<div class="plans">
		<article class="plan">
			<div class="plan-head">
				<span>{t('split.oss_title')}</span>
			</div>
			<p class="plan-price">
				<span class="amount amount-word">{t('pricing.free')}</span>
			</p>
			<ul>
				{#each ossBullets as bullet (bullet)}
					<li>
						<CheckIcon aria-hidden="true" />
						{t(bullet)}
					</li>
				{/each}
			</ul>
			<a class="text-link" href={resolve('/docs/guide/introduction')}>
				{t('pricing.install_guide')}
			</a>
		</article>

		{#each plans as plan (plan.key)}
			<article class="plan" class:featured={plan.featured}>
				<div class="plan-head">
					<span>{t(`pricing.${plan.key}_name`)}</span>
					{#if plan.featured}
						<span class="badge">{t('pricing.t2_badge')}</span>
					{/if}
				</div>
				<p class="plan-price">
					{#if plan.price !== null}
						<span class="amount">&euro;{plan.price}</span>
						<span class="term">{t('pricing.month')}</span>
					{:else}
						<span class="amount amount-word">{t('pricing.t4_price')}</span>
					{/if}
				</p>
				<ul>
					{#each bullets as bullet (bullet)}
						<li>
							<CheckIcon aria-hidden="true" />
							{t(`pricing.${plan.key}_${bullet}`)}
						</li>
					{/each}
				</ul>
				{#if plan.price === null}
					<a class="text-link" href="mailto:admin@leonfuller.com">{t('footer.contact')}</a>
				{/if}
			</article>
		{/each}
	</div>

	<p class="trial">{t('pricing.trial')}</p>

	{#if !full}
		<p>
			<a class="text-link" href={resolve('/pricing')}>{t('pricing.compare_link')}</a>
		</p>
	{:else}
		<div class="estimate">
			<div class="estimate-row">
				<label for="pricing-hours">{t('pricing.estimate_q')}</label>
				<span class="estimate-value">{hoursText}</span>
			</div>
			<input
				id="pricing-hours"
				type="range"
				min="0"
				max="30"
				step="0.5"
				bind:value={hours}
				aria-describedby="pricing-result"
			/>
			<div class="ticks" aria-hidden="true">
				{#each ticks as tick (tick)}
					<span>{tick}</span>
				{/each}
			</div>
			<p id="pricing-result" class="result" aria-live="polite">
				{#if estimate}
					{estimate}
				{:else}
					{t('pricing.estimate_over').replace('{hours}', formatNumber(studioHours))}
					<a class="text-link" href="mailto:admin@leonfuller.com">{t('pricing.t4_price')}</a>
				{/if}
			</p>
		</div>

		<section class="compare" aria-labelledby="pricing-compare-title">
			<h3 id="pricing-compare-title">{t('pricing.cmp_title')}</h3>
			<div class="table-wrap" role="region" aria-label={t('pricing.cmp_title')} tabindex="0">
				<table>
					<caption class="sr-only">{t('pricing.cmp_title')}</caption>
					<thead>
						<tr>
							<th scope="col"></th>
							<th scope="col">{t('split.oss_title')}</th>
							{#each plans as plan (plan.key)}
								<th scope="col">{t(`pricing.${plan.key}_name`)}</th>
							{/each}
						</tr>
					</thead>
					<tbody>
						<tr>
							<th scope="row">{t('pricing.cmp_price')}</th>
							<td>{t('pricing.free')}</td>
							{#each plans as plan (plan.key)}
								<td>
									{#if plan.price !== null}
										&euro;{plan.price}
									{:else}
										{t('pricing.t4_price')}
									{/if}
								</td>
							{/each}
						</tr>
						<tr>
							<th scope="row">{t('pricing.cmp_credits')}</th>
							<td>{t('pricing.cmp_no_limit')}</td>
							{#each plans as plan (plan.key)}
								<td>
									{plan.credits === null ? t('pricing.t4_price') : formatNumber(plan.credits, 0)}
								</td>
							{/each}
						</tr>
						<tr>
							<th scope="row">{t('pricing.cmp_hours')}</th>
							<td>{t('pricing.cmp_gpu_time')}</td>
							{#each plans as plan (plan.key)}
								<td>
									{plan.hours === null
										? t('pricing.t4_price')
										: t('pricing.cmp_hours_about').replace('{hours}', formatNumber(plan.hours))}
								</td>
							{/each}
						</tr>
						<tr>
							<th scope="row">{t('pricing.cmp_priority')}</th>
							<td>-</td>
							{#each plans as plan (plan.key)}
								<td>
									{#if plan.key === 't2'}
										{t('pricing.cmp_planned')}
									{:else if plan.key === 't3'}
										{t('pricing.cmp_planned_top')}
									{:else}
										-
									{/if}
								</td>
							{/each}
						</tr>
						<tr>
							<th scope="row">{t('pricing.cmp_invoicing')}</th>
							<td>-</td>
							<td>-</td>
							<td>-</td>
							<td>-</td>
							<td>{t('pricing.cmp_included')}</td>
						</tr>
						<tr>
							<th scope="row">{t('pricing.cmp_support')}</th>
							<td>-</td>
							<td>-</td>
							<td>-</td>
							<td>-</td>
							<td>{t('pricing.cmp_included')}</td>
						</tr>
					</tbody>
				</table>
			</div>
		</section>

		<section class="billing" aria-labelledby="pricing-billing-title">
			<h3 id="pricing-billing-title">{t('pricing.billing_title')}</h3>
			<div class="questions">
				{#each billingQuestions as item (item.question)}
					<details name="pricing-billing">
						<summary>
							<span>{t(item.question)}</span>
							<span class="toggle" aria-hidden="true">
								<ChevronDownIcon />
							</span>
						</summary>
						<p>{t(item.answer)}</p>
					</details>
				{/each}
			</div>
		</section>
	{/if}
</section>

<style>
	.pricing {
		display: grid;
		gap: clamp(1.75rem, 4vw, 3rem);
		width: 100%;
		max-width: 78rem;
		min-width: 0;
		margin-inline: auto;
		padding: clamp(3.5rem, 9vw, 6.5rem) clamp(1rem, 4vw, 3rem);
	}

	.head {
		display: grid;
		gap: 0.9rem;
		justify-items: center;
		max-width: 56ch;
		margin-inline: auto;
		text-align: center;
	}

	h2 {
		font-size: clamp(1.9rem, 3.8vw, 3.1rem);
		line-height: 1.02;
	}

	h3 {
		font-size: 1.15rem;
		font-weight: 700;
	}

	.head p,
	.trial {
		color: var(--k-muted);
	}

	.plans {
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		gap: 1rem;
	}

	.plan {
		display: grid;
		align-content: start;
		gap: 0.9rem;
		min-width: 0;
		padding: clamp(1rem, 1.6vw, 1.5rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
	}

	.plan.featured {
		border-color: var(--k-accent);
	}

	.plan-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem 0.75rem;
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.badge {
		padding: 0.2rem 0.6rem;
		border-radius: 999px;
		background: var(--k-accent);
		color: var(--k-accent-ink);
		font-size: 0.72rem;
		font-weight: 700;
		white-space: nowrap;
	}

	.plan-price {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.45rem;
	}

	.amount {
		font-size: clamp(1.9rem, 2.4vw, 2.3rem);
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		letter-spacing: -0.04em;
	}

	.amount-word {
		font-size: clamp(1.5rem, 2vw, 1.9rem);
	}

	.term {
		color: var(--k-muted);
		font-size: 0.9rem;
	}

	.plan ul {
		display: grid;
		gap: 0.9rem;
		margin: 0;
		padding: 0;
		list-style: none;
	}

	.plan li {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		gap: 0.6rem;
		align-items: start;
		color: var(--k-muted);
		font-size: 0.92rem;
		line-height: 1.55;
	}

	.plan li :global(svg) {
		width: 1rem;
		height: 1rem;
		flex: none;
		margin-block-start: 0.25rem;
		color: var(--k-accent);
	}

	.text-link {
		justify-self: start;
		color: var(--k-accent);
		font-weight: 700;
		text-decoration: underline;
		text-underline-offset: 0.25em;
	}

	.result a {
		font-weight: 700;
	}

	.estimate {
		display: grid;
		gap: 0.9rem;
		min-width: 0;
		padding: clamp(1.25rem, 3vw, 2rem);
		border: 1px solid var(--k-line);
		border-radius: 1.25rem;
		background: var(--k-panel);
	}

	.estimate-row {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem 1rem;
		font-size: 1.05rem;
		font-weight: 600;
	}

	.estimate-value {
		font-variant-numeric: tabular-nums;
	}

	input[type='range'] {
		width: 100%;
		accent-color: var(--k-accent);
	}

	input[type='range']:focus-visible {
		outline: 2px solid var(--k-focus);
		outline-offset: 4px;
	}

	.ticks {
		display: flex;
		justify-content: space-between;
		color: var(--k-muted);
		font-size: 0.8rem;
		font-variant-numeric: tabular-nums;
	}

	.result {
		color: var(--k-ink);
		font-size: 0.98rem;
		line-height: 1.5;
	}

	.table-wrap {
		max-width: 100%;
		overflow-x: auto;
		border-radius: 0.75rem;
	}

	.table-wrap:focus-visible {
		outline: 2px solid var(--k-focus);
		outline-offset: 3px;
	}

	.compare {
		display: grid;
		gap: 1rem;
		min-width: 0;
	}

	table {
		width: 100%;
		min-width: 40rem;
		border-collapse: collapse;
		font-size: 0.92rem;
	}

	th,
	td {
		height: 3rem;
		padding: 0.5rem 0.6rem;
		border-block-end: 1px solid var(--k-line);
		text-align: center;
		vertical-align: middle;
	}

	thead th {
		font-weight: 700;
	}

	tbody th {
		color: var(--k-ink);
		font-weight: 500;
		text-align: start;
	}

	tbody td {
		color: var(--k-muted);
	}

	.billing {
		display: grid;
		gap: 1rem;
		min-width: 0;
	}

	.questions {
		border-block-start: 1px solid var(--k-line);
	}

	details {
		border-block-end: 1px solid var(--k-line);
	}

	summary {
		display: grid;
		grid-template-columns: minmax(0, 1fr) 2.25rem;
		align-items: center;
		gap: 1rem;
		min-height: 4rem;
		padding-block: 0.9rem;
		cursor: pointer;
		list-style: none;
		font-size: 1.05rem;
		font-weight: 700;
		line-height: 1.25;
	}

	summary::-webkit-details-marker {
		display: none;
	}

	summary:focus-visible {
		outline: 2px solid var(--k-focus);
		outline-offset: 2px;
	}

	.toggle {
		display: grid;
		width: 2.25rem;
		aspect-ratio: 1;
		place-items: center;
		border: 1px solid var(--k-line);
		border-radius: 50%;
		color: var(--k-muted);
		transition:
			border-color 180ms var(--k-ease),
			color 180ms var(--k-ease),
			transform 240ms var(--k-ease);
	}

	.toggle :global(svg) {
		width: 1rem;
		height: 1rem;
	}

	details[open] .toggle {
		border-color: var(--k-accent);
		color: var(--k-accent);
		transform: rotate(180deg);
	}

	details > p {
		max-width: 62ch;
		padding: 0 0 1.5rem;
		color: var(--k-muted);
		font-size: 0.98rem;
	}

	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}

	/* Five plans in one row from 1100px, then 3, 2 and one column. */
	@media (min-width: 40rem) {
		.plans {
			grid-template-columns: repeat(2, minmax(0, 1fr));
		}
	}

	@media (min-width: 64rem) {
		.plans {
			grid-template-columns: repeat(3, minmax(0, 1fr));
		}
	}

	@media (min-width: 68.75rem) {
		.plans {
			grid-template-columns: repeat(5, minmax(0, 1fr));
			gap: 0.5rem;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.toggle {
			transition: none;
		}
	}

	@media (hover: hover) and (pointer: fine) {
		summary:hover .toggle {
			border-color: var(--k-accent);
			color: var(--k-accent);
		}
	}
</style>
