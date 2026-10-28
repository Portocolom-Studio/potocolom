<script lang="ts">
	import type { Snippet } from 'svelte';
	import { resolve } from '$app/paths';
	import ParticleSea from '$lib/components/landing/ParticleSea.svelte';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import LanguageToggle from '$lib/components/LanguageToggle.svelte';

	let {
		fieldSide,
		kicker,
		heading,
		headingTail,
		body,
		children
	}: {
		/** Which side of the page the animated field sits on. */
		fieldSide: 'start' | 'end';
		kicker?: string;
		heading: string;
		/** A second heading line, set in the muted tone. */
		headingTail?: string;
		body?: string;
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
			{#if kicker}<p class="kicker">{kicker}</p>{/if}
			<h2 class="heading">
				<span>{heading}</span>
				{#if headingTail}<span class="tail">{headingTail}</span>{/if}
			</h2>
			{#if body}<p class="body">{body}</p>{/if}
		</div>
	</div>
</main>

<style>
	.split {
		display: flex;
		min-block-size: 100dvh;
		background: #07090f;
		color: #f4f6fb;
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
		color: #f4f6fb;
		font-size: 1.05rem;
		font-weight: 800;
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
		border-inline-start: 1px solid #1c2440;
	}

	.split.field-start .field {
		border-inline-start: 0;
		border-inline-end: 1px solid #1c2440;
	}

	.overlay {
		position: absolute;
		inset: 4.5rem 4rem auto;
		z-index: 1;
		pointer-events: none;
	}

	.kicker {
		margin: 0 0 1rem;
		color: #a9b9ff;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.75rem;
		line-height: 1rem;
		letter-spacing: 0.02em;
	}

	.heading {
		margin: 0;
		color: #f4f6fb;
		font-size: 2rem;
		font-weight: 600;
		line-height: 2.5rem;
		letter-spacing: -0.03em;
	}

	.heading > span {
		display: block;
	}

	.heading .tail {
		color: #9aa4bd;
	}

	.body {
		max-inline-size: 26rem;
		margin: 1rem 0 0;
		color: #9aa4bd;
		font-size: 0.9375rem;
		line-height: 1.6;
	}

	/* Form controls. The primitives keep their markup; these set the colours,
	   sizes and focus ring of the auth page. */
	.form-wrap :global([data-slot='card']) {
		gap: 1.25rem;
		padding: 0;
		border-radius: 0;
		background: transparent;
		box-shadow: none;
		color: #f4f6fb;
		overflow: visible;
	}

	.form-wrap :global([data-slot='card-header']),
	.form-wrap :global([data-slot='card-content']) {
		padding-inline: 0;
	}

	.form-wrap :global([data-slot='card-title']) {
		color: #f4f6fb;
		font-size: 1.5rem;
		font-weight: 600;
		line-height: 2rem;
		letter-spacing: -0.02em;
	}

	.form-wrap :global([data-slot='card-description']),
	.form-wrap :global([data-slot='field-description']),
	.form-wrap :global(.text-muted-foreground) {
		color: #9aa4bd;
	}

	.form-wrap :global(label) {
		color: #f4f6fb;
		font-size: 0.8125rem;
		font-weight: 500;
		line-height: 1.125rem;
	}

	.form-wrap :global([data-slot='input']) {
		block-size: 2.5rem;
		border: 1px solid #263152;
		border-radius: 0.5rem;
		background: #0f1830;
		color: #f4f6fb;
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
		border: 1px solid #4263eb;
		background: #4263eb;
		color: #ffffff;
	}

	.form-wrap :global(button[data-slot='button'][type='submit']:hover:not(:disabled)) {
		border-color: #3451ce;
		background: #3451ce;
	}

	.form-wrap :global(a[data-slot='button']),
	.form-wrap :global(button[data-slot='button']:not([type='submit'])) {
		border: 1px solid #263152;
		background: #0f1830;
		color: #f4f6fb;
	}

	.form-wrap :global(a[data-slot='button']:hover),
	.form-wrap :global(button[data-slot='button']:not([type='submit']):hover) {
		border-color: #3a4a78;
		background: #162447;
	}

	.form-wrap :global(a:not([data-slot])) {
		color: #a9b9ff;
		text-decoration: underline;
		text-underline-offset: 0.25em;
	}

	.form-wrap :global(input[type='checkbox']) {
		accent-color: #4263eb;
	}

	.split :global(:focus-visible) {
		outline: 2px solid #5b7bff;
		outline-offset: 2px;
	}

	/* Below 56rem the field is a band above the form. The brand row moves to
	   the top of the page and the overlay keeps only the optional kicker. */
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
			flex: 0 0 12rem;
			border-inline-start: 0;
			border-block-end: 1px solid #1c2440;
		}

		.split.field-start .field {
			border-inline-end: 0;
		}

		.form-wrap {
			order: 0;
			padding: 0 1.25rem 2.5rem;
		}

		.overlay {
			inset: 1.5rem 1.25rem auto;
		}

		.heading,
		.body {
			display: none;
		}
	}
</style>
