<script module lang="ts">
	export type LandingAsset = {
		src: string;
		srcset: string;
		sizes: string;
	};

	export type LandingEntrancePhase = 'loading' | 'revealing' | 'ready';
</script>

<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import { onMount } from 'svelte';

	let {
		assets,
		onphase
	}: {
		assets: LandingAsset[];
		onphase?: (phase: LandingEntrancePhase) => void;
	} = $props();

	let phase: LandingEntrancePhase = $state('loading');
	let progress = $state(0);
	let visible = $state(true);

	function preload(asset: LandingAsset): Promise<void> {
		return new Promise((resolve) => {
			const image = new Image();
			let settled = false;
			let decoding = false;

			const finish = () => {
				if (settled) return;
				settled = true;
				resolve();
			};
			const decode = () => {
				if (settled || decoding) return;
				if (typeof image.decode !== 'function') {
					finish();
					return;
				}
				decoding = true;
				void image
					.decode()
					.catch(() => undefined)
					.finally(finish);
			};

			image.onload = decode;
			image.onerror = finish;
			image.decoding = 'async';
			image.sizes = asset.sizes;
			image.srcset = asset.srcset;
			image.src = asset.src;

			if (image.complete) {
				if (image.naturalWidth > 0) decode();
				else finish();
			}
		});
	}

	onMount(() => {
		let cancelled = false;
		const timers = new Set<number>();
		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const minimumSpinMs = reduced ? 0 : 900;
		const revealMs = reduced ? 120 : 1500;
		const unique = [
			...new Map(assets.map((asset) => [`${asset.srcset}|${asset.sizes}`, asset])).values()
		];

		const wait = (duration: number) =>
			new Promise<void>((resolve) => {
				const timer = window.setTimeout(() => {
					timers.delete(timer);
					resolve();
				}, duration);
				timers.add(timer);
			});

		let complete = 0;
		if (unique.length === 0) progress = 100;

		let next = 0;
		const loadNext = async () => {
			while (next < unique.length) {
				const asset = unique[next];
				next += 1;
				await preload(asset);
				if (cancelled) return;
				complete += 1;
				progress = Math.round((complete / unique.length) * 100);
			}
		};
		const loading = Promise.all(
			Array.from({ length: Math.min(8, unique.length) }, () => loadNext())
		);

		void Promise.all([wait(minimumSpinMs), loading]).then(async () => {
			if (cancelled) return;
			phase = 'revealing';
			onphase?.(phase);
			await wait(revealMs);
			if (cancelled) return;
			phase = 'ready';
			onphase?.(phase);
			visible = false;
		});

		return () => {
			cancelled = true;
			for (const timer of timers) window.clearTimeout(timer);
		};
	});
</script>

{#if visible}
	<div
		class="landing-loader"
		class:revealing={phase === 'revealing'}
		role="status"
		aria-live="polite"
		aria-label={t('loader.loading')}
	>
		<div class="loader-rings" aria-hidden="true">
			<span class="ring ring-one"></span>
			<span class="ring ring-two"></span>
			<span class="ring ring-three"></span>
			<span class="ring ring-four"></span>
		</div>
		<div
			class="readout"
			role="progressbar"
			aria-label={t('loader.loading')}
			aria-valuemin="0"
			aria-valuemax="100"
			aria-valuenow={progress}
		>
			<strong>{progress}%</strong>
			<span>{t('loader.loading')}</span>
		</div>
	</div>
{/if}

<style>
	/* The four rings expand from this compact loader into the matching
	   production orbit geometry while the overlay fades. */
	.landing-loader {
		position: fixed;
		inset: 0;
		z-index: 300;
		display: grid;
		place-items: center;
		overflow: clip;
		background: var(--k-paper);
		color: var(--k-ink);
		opacity: 1;
		transition: opacity 760ms var(--k-ease) 520ms;
	}

	.loader-rings {
		position: absolute;
		width: 10rem;
		aspect-ratio: 1;
		opacity: 1;
		transform: scale(1);
		transition:
			opacity 700ms var(--k-ease) 580ms,
			transform 1500ms var(--k-ease-in-out);
	}

	.ring {
		position: absolute;
		border: 1px solid var(--k-line);
		border-radius: 50%;
		animation: loader-turn 1.8s linear infinite;
	}

	.ring::after {
		position: absolute;
		inset-block-start: -0.24rem;
		inset-inline-start: 50%;
		width: 0.48rem;
		aspect-ratio: 1;
		border-radius: 50%;
		background: var(--k-accent);
		box-shadow: 0 0 1rem color-mix(in oklch, var(--k-accent) 55%, transparent);
		content: '';
		transform: translateX(-50%);
	}

	.ring-one {
		inset: 2.24rem;
		animation-duration: 1.15s;
	}

	.ring-two {
		inset: 1.49rem;
		animation-direction: reverse;
		animation-duration: 1.55s;
	}

	.ring-three {
		inset: 0.74rem;
		animation-duration: 2.1s;
	}

	.ring-four {
		inset: 0;
		animation-direction: reverse;
		animation-duration: 2.8s;
	}

	.readout {
		position: relative;
		z-index: 1;
		display: grid;
		justify-items: center;
		gap: 0.3rem;
		opacity: 1;
		transform: scale(1);
		transition:
			opacity 240ms var(--k-ease),
			transform 240ms var(--k-ease);
	}

	.readout strong {
		font-size: 1rem;
		font-variant-numeric: tabular-nums;
		letter-spacing: -0.03em;
	}

	.readout span {
		color: var(--k-muted);
		font-size: 0.72rem;
		letter-spacing: 0.03em;
	}

	.revealing {
		opacity: 0;
		pointer-events: none;
	}

	.revealing .loader-rings {
		opacity: 0;
		transform: scale(7.24);
	}

	.revealing .readout {
		opacity: 0;
		transform: scale(0.92);
	}

	@keyframes loader-turn {
		to {
			transform: rotate(360deg);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.landing-loader,
		.loader-rings,
		.readout {
			transition-duration: 120ms;
			transition-delay: 0ms;
		}

		.ring {
			animation: none;
		}

		.revealing .loader-rings,
		.revealing .readout {
			transform: none;
		}
	}
</style>
