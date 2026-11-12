<script lang="ts">
	import type { Snippet } from 'svelte';
	import { resolve } from '$app/paths';
	import ParticleSea from '$lib/components/landing/ParticleSea.svelte';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';

	let {
		fieldSide,
		heading,
		headingTail,
		children
	}: {
		/** Which side of the page the animated field sits on. */
		fieldSide: 'start' | 'end';
		heading: string;
		/** A second heading line, set in the muted tone. */
		headingTail?: string;
		children: Snippet;
	} = $props();
</script>

<main class="split" class:field-start={fieldSide === 'start'}>
	<div class="form-col">
		<div class="form-top">
			<a class="mark" href={resolve('/')}><BrandMark /></a>
			<LanguageToggle />
		</div>
		<div class="form-wrap">
			{@render children()}
		</div>
	</div>
	<div class="field">
		<ParticleSea variant="field" />
		<div class="overlay">
			<h2 class="heading">
				<span>{heading}</span>
				{#if headingTail}<span class="tail">{headingTail}</span>{/if}
			</h2>
		</div>
	</div>
</main>

<style>
	.split {
		display: flex;
		min-block-size: 100dvh;
		background: var(--background);
		color: var(--foreground);
	}

	.split.field-start {
		flex-direction: row-reverse;
	}

	.form-col {
		display: flex;
		flex: 1 1 35rem;
		flex-direction: column;
		min-inline-size: 0;
		max-inline-size: 35rem;
		padding: 2rem 2.5rem;
	}

	.form-top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 1rem;
	}

	.mark {
		color: var(--foreground);
		font-size: 1.05rem;
		font-weight: 700;
		letter-spacing: -0.03em;
		text-decoration: none;
	}

	.form-wrap {
		display: flex;
		flex: 1;
		flex-direction: column;
		justify-content: center;
		inline-size: 100%;
		max-inline-size: 22.5rem;
	}

	.field {
		position: relative;
		overflow: hidden;
		display: flex;
		flex: 999 1 30rem;
		min-inline-size: 0;
		border-inline-start: 1px solid var(--border);
	}

	.split.field-start .field {
		border-inline-start: 0;
		border-inline-end: 1px solid var(--border);
	}

	.overlay {
		position: absolute;
		inset: 4.5rem 4rem auto;
		z-index: 1;
		pointer-events: none;
	}

	.heading {
		margin: 0;
		color: var(--foreground);
		font-size: 2rem;
		font-weight: 600;
		line-height: 2.5rem;
		letter-spacing: -0.03em;
	}

	.heading > span {
		display: block;
	}

	.heading .tail {
		color: var(--muted-foreground);
	}

	/* Form controls. The primitives keep their markup; these set the colours,
	   sizes and focus ring of the auth page. */
	.form-wrap :global([data-slot='card']) {
		gap: 1.25rem;
		padding: 0;
		border-radius: 0;
		background: transparent;
		box-shadow: none;
		color: var(--foreground);
		overflow: visible;
	}

	.form-wrap :global([data-slot='card-header']),
	.form-wrap :global([data-slot='card-content']) {
		padding-inline: 0;
	}

	.form-wrap :global([data-slot='card-title']) {
		color: var(--foreground);
		font-size: 1.5rem;
		font-weight: 600;
		line-height: 2rem;
		letter-spacing: -0.02em;
	}

	.form-wrap :global([data-slot='card-description']),
	.form-wrap :global([data-slot='field-description']),
	.form-wrap :global(.text-muted-foreground) {
		color: var(--muted-foreground);
	}

	.form-wrap :global(label) {
		color: var(--foreground);
		font-size: 0.8125rem;
		font-weight: 500;
		line-height: 1.125rem;
	}

	.form-wrap :global([data-slot='input']) {
		block-size: 2.5rem;
		border: 1px solid var(--line-strong);
		border-radius: 0.5rem;
		background: var(--field);
		color: var(--foreground);
		box-shadow: none;
	}

	.form-wrap :global([data-slot='button']) {
		block-size: 2.5rem;
		inline-size: 100%;
		border-radius: 0.5rem;
		font-size: 0.9375rem;
		font-weight: 600;
	}

	.form-wrap :global(button[data-slot='button'][type='submit']) {
		border: 1px solid var(--brand);
		background: var(--brand);
		color: var(--brand-foreground);
	}

	.form-wrap :global(button[data-slot='button'][type='submit']:hover:not(:disabled)) {
		border-color: #3357d6;
		background: #3357d6;
	}

	.form-wrap :global(a[data-slot='button']),
	.form-wrap :global(button[data-slot='button']:not([type='submit'])) {
		border: 1px solid var(--line-strong);
		background: var(--raised);
		color: var(--foreground);
	}

	.form-wrap :global(a[data-slot='button']:hover),
	.form-wrap :global(button[data-slot='button']:not([type='submit']):hover) {
		border-color: var(--line-strong);
		background: var(--hover);
	}

	.form-wrap :global(a:not([data-slot])) {
		color: #a9b9ff;
		text-decoration: underline;
		text-underline-offset: 0.25em;
	}

	.form-wrap :global(input[type='checkbox']) {
		accent-color: var(--brand);
	}

	.split :global(:focus-visible) {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Below 56rem the field is a short band above the form. The brand row moves
	   to the top of the page and the text overlay is hidden. */
	@media (max-width: 55.99rem) {
		.split,
		.split.field-start {
			flex-direction: column;
		}

		.form-col {
			display: contents;
		}

		.form-top {
			order: -2;
			padding: 1rem 1.25rem;
		}

		.field {
			order: -1;
			flex: 0 0 8rem;
			border-inline-start: 0;
			border-block-end: 1px solid var(--border);
		}

		.split.field-start .field {
			border-inline-end: 0;
		}

		.form-wrap {
			order: 0;
			justify-content: flex-start;
			padding: 1.5rem 1.25rem 2.5rem;
		}

		.overlay {
			inset: 1.5rem 1.25rem auto;
		}

		.heading {
			display: none;
		}
	}
</style>
