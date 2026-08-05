<script lang="ts">
	// Latent-styled waitlist: closing block with the pointer-lit field behind it,
	// and the email form when PUBLIC_WAITLIST_URL is baked in at build time.
	import { PUBLIC_WAITLIST_URL } from '$env/static/public';
	import { resolve } from '$app/paths';
	import LatentCanvas from '$lib/components/LatentCanvas.svelte';
	import type { LatentCanvasApi } from '$lib/latent-canvas-scene';
	import { getLocale, t } from '$lib/i18n.svelte';

	let {
		/** Skip the local field when a page already has a fixed LatentCanvas. */
		field = true
	}: { field?: boolean } = $props();

	const endpoint = PUBLIC_WAITLIST_URL;
	const repoUrl = 'https://github.com/portocolom-studio/potocolom';
	const forkUrl = `${repoUrl}/fork`;

	let email = $state('');
	let honeypot = $state('');
	let status = $state<'idle' | 'sending' | 'done' | 'already' | 'error'>('idle');
	let canvasApi: LatentCanvasApi | null = null;
	let root: HTMLElement | undefined = $state();

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		if (!endpoint || honeypot !== '' || status === 'sending') return;
		status = 'sending';
		try {
			const response = await fetch(endpoint, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ email, locale: getLocale() })
			});
			const contentType = response.headers.get('content-type') ?? '';
			if (!response.ok || !contentType.includes('application/json')) {
				throw new Error(String(response.status));
			}
			const result = (await response.json()) as { status?: string };
			status = result.status === 'exists' ? 'already' : 'done';
		} catch {
			status = 'error';
		}
	}

	function onPointerMove(event: PointerEvent) {
		if (!canvasApi || !root) return;
		const rect = root.getBoundingClientRect();
		canvasApi.setCursor(event.clientX - rect.left, event.clientY - rect.top);
	}

	function onPointerLeave() {
		canvasApi?.setCursor(null, null);
	}

	$effect(() => {
		if (!field || !root) return;
		const node = root;
		node.addEventListener('pointermove', onPointerMove);
		node.addEventListener('pointerleave', onPointerLeave);
		return () => {
			node.removeEventListener('pointermove', onPointerMove);
			node.removeEventListener('pointerleave', onPointerLeave);
		};
	});
</script>

<section id="waitlist" class="closing" bind:this={root} aria-labelledby="waitlist-title">
	{#if field}
		<div class="field" aria-hidden="true">
			<LatentCanvas
				followCursor
				animate
				warmupFrames={400}
				onAttach={(api) => {
					canvasApi = api;
				}}
			/>
		</div>
		<div class="veil" aria-hidden="true"></div>
	{/if}

	<div class="body">
		<h2 id="waitlist-title">{t('wl.title')}</h2>
		<p>{t('wl.sub')}</p>

		{#if endpoint}
			{#if status === 'done' || status === 'already'}
				<p class="note ok" role="status">{status === 'done' ? t('wl.done') : t('wl.already')}</p>
			{:else}
				<form onsubmit={submit}>
					<label class="sr-only" for="waitlist-email">{t('wl.email_label')}</label>
					<input
						id="waitlist-email"
						type="email"
						name="email"
						required
						placeholder={t('wl.placeholder')}
						bind:value={email}
						disabled={status === 'sending'}
					/>
					<input
						type="text"
						name="website"
						class="honey"
						tabindex="-1"
						autocomplete="off"
						aria-hidden="true"
						bind:value={honeypot}
					/>
					<button class="pill pill-accent" type="submit" disabled={status === 'sending'}>
						{status === 'sending' ? t('wl.sending') : t('wl.cta')}
					</button>
				</form>
				{#if status === 'error'}
					<p class="note err" role="alert">{t('wl.error')}</p>
				{/if}
			{/if}
			<a class="privacy" href={resolve('/privacy')}>{t('wl.privacy_note')}</a>
		{:else}
			<div class="actions">
				<a class="pill pill-accent" href={resolve('/app')}>{t('hero.cta_launch')}</a>
				<a class="pill pill-ghost" href={forkUrl}>{t('fork.cta_fork')}</a>
			</div>
		{/if}
	</div>
</section>

<style>
	.closing {
		position: relative;
		isolation: isolate;
		overflow: clip;
		padding: clamp(3rem, 8vw, 6rem) clamp(1rem, 4vw, 3rem);
		text-align: center;
	}

	.field,
	.veil {
		position: absolute;
		inset: 0;
		z-index: 0;
		pointer-events: none;
	}

	/* Same paper as the self-host / cloud particle stage. The strokes use
	   additive blending, so a solid black cover (or a heavy veil) kills them;
	   tint the canvas clear to paper and keep the veil light. */
	.field {
		--latent-clear: var(--k-paper);
		--latent-fade: oklch(0.08 0.012 265 / 4.5%);
		background: var(--k-paper);
	}

	:global(:root[data-landing-mode='light']) .field {
		--latent-fade: oklch(0.97 0.004 255 / 5%);
	}

	.field :global(canvas) {
		width: 100%;
		height: 100%;
	}

	.veil {
		background:
			radial-gradient(38% 46% at 50% 42%, oklch(0.62 0.2 255 / 12%) 0%, transparent 72%),
			radial-gradient(70% 60% at 50% 50%, transparent 0%, oklch(0.08 0.012 265 / 22%) 92%);
	}

	:global(:root[data-landing-mode='light']) .veil {
		background:
			radial-gradient(38% 46% at 50% 42%, oklch(0.62 0.2 255 / 10%) 0%, transparent 72%),
			radial-gradient(70% 60% at 50% 50%, transparent 0%, oklch(0.97 0.004 255 / 28%) 92%);
	}

	.body {
		position: relative;
		z-index: 1;
		display: grid;
		justify-items: center;
		gap: 1.1rem;
	}

	h2 {
		font-size: clamp(1.9rem, 4vw, 3.2rem);
		line-height: 1.02;
	}

	p {
		max-width: 48ch;
		color: var(--k-muted);
		font-size: 1.02rem;
	}

	form {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
		width: min(28rem, 100%);
		margin-block-start: 0.4rem;
	}

	input[type='email'] {
		flex: 1 1 12rem;
		min-width: 0;
		height: 2.75rem;
		padding: 0 1rem;
		border: 1px solid var(--k-line);
		border-radius: 999px;
		background: var(--k-panel);
		color: inherit;
		font: inherit;
	}

	input[type='email']:focus-visible {
		outline: 2px solid var(--k-accent);
		outline-offset: 2px;
	}

	.honey {
		position: absolute;
		left: -9999px;
		width: 1px;
		height: 1px;
		opacity: 0;
	}

	.pill {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.45rem;
		height: 2.75rem;
		padding: 0 1.25rem;
		border: 1px solid transparent;
		border-radius: 999px;
		font-size: 0.92rem;
		font-weight: 600;
		text-decoration: none;
		cursor: pointer;
	}

	.pill:disabled {
		opacity: 0.6;
		cursor: wait;
	}

	.pill-accent {
		background: var(--k-accent);
		color: oklch(0.14 0.02 265);
	}

	.pill-ghost {
		border-color: var(--k-line);
		background: transparent;
		color: inherit;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		gap: 0.7rem;
		margin-block-start: 0.4rem;
	}

	.note {
		margin: 0;
		font-size: 0.95rem;
	}

	.note.ok {
		color: var(--k-accent);
	}

	.note.err {
		color: oklch(0.7 0.16 25);
	}

	.privacy {
		color: var(--k-muted);
		font-size: 0.78rem;
		text-decoration: underline;
		text-underline-offset: 0.2em;
	}

	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}

	@media (max-width: 30rem) {
		form {
			flex-direction: column;
			align-items: stretch;
		}

		.pill {
			width: 100%;
		}
	}
</style>
