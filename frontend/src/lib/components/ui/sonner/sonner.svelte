<script lang="ts">
	import { Toaster as Sonner, type ToasterProps as SonnerProps } from 'svelte-sonner';

	// The studio is dark only, so the toaster is too; its colours come from the
	// same tokens as popovers. svelte-sonner ships its styles inside the
	// component, so they are bundled with the app and need no CSP change.
	let { ...restProps }: SonnerProps = $props();
</script>

<Sonner
	theme="dark"
	class="toaster group"
	style="--normal-bg: var(--color-popover); --normal-text: var(--color-popover-foreground); --normal-border: var(--color-line-strong);"
	{...restProps}
/>
