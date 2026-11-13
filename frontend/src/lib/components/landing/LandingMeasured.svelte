<script lang="ts">
	import { resolve } from '$app/paths';
	import { getLocale, t } from '$lib/i18n.svelte';
	import { measured } from '$lib/landing-measured';

	const leader = measured.rows[0];
	const longest = Math.max(...measured.rows.map((row) => row.seconds));

	const jobs = $derived(new Intl.NumberFormat(getLocale()).format(measured.totalJobs));
	const body = $derived(
		t('measured.body')
			.replace('{model}', leader.name)
			.replace('{gpu}', measured.gpu)
			.replace('{jobs}', jobs)
	);

	function seconds(value: number): string {
		return new Intl.NumberFormat(getLocale(), {
			minimumFractionDigits: 1,
			maximumFractionDigits: 1
		}).format(value);
	}
</script>

<section id="measured" class="measured" aria-labelledby="measured-title">
	<div class="story">
		<h2 id="measured-title">{t('measured.title')}</h2>
		<p class="figure">
			<span class="value">{seconds(leader.seconds)}</span><span class="unit"
				>{t('measured.unit')}</span
			>
		</p>
		<p class="lede">{body}</p>
	</div>

	<div class="bars">
		<table>
			<caption class="sr-only">{t('measured.caption')}</caption>
			<tbody>
				{#each measured.rows as row, index (row.id)}
					<tr class:leader={index === 0}>
						<th scope="row">{row.name}</th>
						<td>
							<span class="bar" style:width={`${(row.seconds / longest) * 100}%`}>
								{seconds(row.seconds)}
								{t('measured.unit')}
							</span>
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
		<a href={resolve('/benchmark')}>{t('measured.link')}</a>
	</div>
</section>

<style>
	.measured {
		position: relative;
		z-index: 1;
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: clamp(2.5rem, 5vw, 4rem);
		align-items: end;
		max-width: 84rem;
		margin-inline: auto;
		padding: clamp(4.5rem, 10vw, 9rem) 3rem;
	}

	h2 {
		font-size: clamp(2.6rem, 6vw, 5.5rem);
		line-height: 0.94;
	}

	.figure {
		display: flex;
		align-items: baseline;
		margin-block-start: clamp(2.5rem, 5vw, 4rem);
		margin-inline-start: -0.04em;
	}

	.value {
		font-size: clamp(6rem, 19vw, 17.5rem);
		font-weight: 700;
		line-height: 0.8;
		letter-spacing: -0.07em;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}

	.unit {
		margin-inline-start: 0.1em;
		color: var(--k-muted);
		font-size: 0.43em;
		font-weight: 700;
		letter-spacing: -0.04em;
	}

	.lede {
		max-width: 34rem;
		margin-block-start: 2.25rem;
		color: var(--k-muted);
		font-size: 1rem;
	}

	table {
		width: 100%;
		border-collapse: separate;
		border-spacing: 0 0.875rem;
		font-variant-numeric: tabular-nums;
	}

	th,
	td {
		padding: 0;
		vertical-align: middle;
	}

	th {
		width: 10rem;
		padding-inline-end: 1rem;
		color: var(--k-muted);
		font-size: 0.94rem;
		font-weight: 400;
		text-align: start;
		line-height: 1.3;
	}

	tr.leader th {
		color: var(--k-ink);
		font-weight: 600;
	}

	.bar {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		box-sizing: border-box;
		height: 2rem;
		min-width: 4.5rem;
		padding-inline: 0.75rem;
		border-radius: 6px;
		background: var(--k-panel);
		color: var(--k-ink);
		font-size: 0.9rem;
		font-weight: 500;
		white-space: nowrap;
	}

	tr.leader .bar {
		background: var(--k-accent);
		color: var(--k-accent-ink);
	}

	.bars a {
		display: inline-flex;
		margin-block-start: 1.75rem;
		color: var(--k-ink);
		font-weight: 500;
		text-decoration: underline;
		text-decoration-color: var(--k-accent);
		text-underline-offset: 0.25em;
	}

	.bars a:hover {
		text-decoration-thickness: 2px;
	}

	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		margin: -1px;
		padding: 0;
		overflow: hidden;
		clip: rect(0 0 0 0);
		border: 0;
		white-space: nowrap;
	}

	@media (max-width: 860px) {
		.measured {
			grid-template-columns: minmax(0, 1fr);
			gap: 2.5rem;
			padding: clamp(3.5rem, 12vw, 4.5rem) 1.25rem;
		}

		.figure {
			margin-block-start: 2rem;
		}

		.value {
			font-size: clamp(6rem, 30vw, 9rem);
		}

		th {
			width: 7rem;
			font-size: 0.875rem;
		}
	}
</style>
