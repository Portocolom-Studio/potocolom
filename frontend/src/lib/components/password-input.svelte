<script lang="ts">
	import type { HTMLInputAttributes } from 'svelte/elements';
	import Eye from '@lucide/svelte/icons/eye';
	import EyeOff from '@lucide/svelte/icons/eye-off';
	import { Input } from '$lib/components/ui/input';
	import { passwordHint, t } from '$lib/i18n.svelte';

	let {
		id,
		value = $bindable(''),
		autocomplete,
		'aria-describedby': describedBy,
		...rest
	}: Omit<HTMLInputAttributes, 'type' | 'value' | 'id' | 'files'> & {
		id: string;
		value?: string;
	} = $props();
	let revealed = $state(false);
	const hintId = $derived(`${id}-hint`);
	const description = $derived(
		[describedBy, autocomplete === 'new-password' ? hintId : undefined].filter(Boolean).join(' ') ||
			undefined
	);
</script>

<div class="relative">
	<Input
		{id}
		{autocomplete}
		{...rest}
		aria-describedby={description}
		type={revealed ? 'text' : 'password'}
		bind:value
		class="pr-10"
	/>
	<button
		type="button"
		class="text-muted-foreground hover:text-foreground focus-visible:ring-ring absolute inset-y-0 right-1 flex w-9 items-center justify-center rounded-full focus-visible:ring-2 focus-visible:outline-none disabled:opacity-50"
		aria-label={t(revealed ? 'auth.password.hide' : 'auth.password.show')}
		aria-pressed={revealed}
		aria-controls={id}
		disabled={rest.disabled}
		onmousedown={(event) => event.preventDefault()}
		onclick={() => (revealed = !revealed)}
	>
		{#if revealed}<EyeOff size={18} aria-hidden="true" />{:else}<Eye
				size={18}
				aria-hidden="true"
			/>{/if}
	</button>
</div>
{#if autocomplete === 'new-password'}
	<p id={hintId} class="text-muted-foreground text-sm">{passwordHint()}</p>
{/if}
