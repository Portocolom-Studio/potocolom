<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import { t } from '$lib/i18n.svelte';
	import {
		bannerDefaultMessageKey,
		bannerDismissKey,
		bannerTone,
		bannerVisible,
		type BannerConfig
	} from '$lib/status-banner-logic';

	const DISMISS_KEY = 'potocolom_banner_dismissed';

	let banner = $state<BannerConfig>(null);
	let dismissed = $state<string | null>(null);

	// sessionStorage access itself can throw where the browser blocks site
	// data (a SecurityError, not just an absent global), so every access is
	// wrapped rather than guarded with a typeof check (safe-storage.ts has
	// the same shape for localStorage).
	function readDismissed(): string | null {
		try {
			return sessionStorage.getItem(DISMISS_KEY);
		} catch {
			return null;
		}
	}

	function writeDismissed(value: string): void {
		try {
			sessionStorage.setItem(DISMISS_KEY, value);
		} catch {}
	}

	onMount(async () => {
		dismissed = readDismissed();
		try {
			const response = await apiFetch('/api/v1/config');
			if (!response.ok) return;
			const config = (await response.json()) as { banner?: BannerConfig };
			banner = config.banner ?? null;
		} catch {
			banner = null;
		}
	});

	const visible = $derived(bannerVisible(banner, dismissed));
	const tone = $derived(banner ? bannerTone(banner.kind) : 'muted');
	const text = $derived(
		banner
			? (banner.custom_text ??
					t(
						(banner.message_key ?? bannerDefaultMessageKey(banner.kind)) as Parameters<typeof t>[0]
					))
			: ''
	);

	function dismiss(): void {
		if (banner === null) return;
		const key = bannerDismissKey(banner);
		dismissed = key;
		writeDismissed(key);
	}
</script>

{#if visible}
	<div
		role="status"
		aria-live="polite"
		class="mb-3 flex items-center gap-2 rounded-md px-3 py-2 text-sm {tone === 'destructive'
			? 'bg-destructive/10 text-destructive border-destructive/30 border'
			: 'bg-muted'}"
	>
		<span class="flex-1">{text}</span>
		<button type="button" class="shrink-0 underline-offset-2 hover:underline" onclick={dismiss}>
			{t('app.banner.dismiss')}
		</button>
	</div>
{/if}
