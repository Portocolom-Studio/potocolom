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
		hint = false,
		toggleLabel = 'auth.password.show',
		'aria-describedby': describedBy,
		...rest
	}: Omit<HTMLInputAttributes, 'type' | 'value' | 'id' | 'files'> & {
		id: string;
		value?: string;
		hint?: boolean;
		toggleLabel?:
			| 'auth.password.show'
			| 'auth.password.show_confirmation'
			| 'auth.password.show_current'
			| 'auth.password.show_new';
	} = $props();
	let revealed = $state(false);
	const hintId = $derived(`${id}-hint`);
	const description = $derived(
		[describedBy, hint ? hintId : undefined].filter(Boolean).join(' ') || undefined
	);
</script>

<div class="relative">
	<Input
		{id}
		{autocomplete}
		{...rest}
		aria-describedby={description}
		type={revealed ? 'text' : 'password'}
		autocapitalize="none"
		autocorrect="off"
		spellcheck={false}
		bind:value
		class="pr-10"
	/>
	<button
		type="button"
		class="text-muted-foreground hover:text-foreground focus-visible:ring-ring absolute inset-y-0 right-1 flex w-9 items-center justify-center rounded-full focus-visible:ring-2 focus-visible:outline-none disabled:opacity-50"
		aria-label={t(toggleLabel)}
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
{#if hint}
	<p id={hintId} class="text-muted-foreground text-sm">{passwordHint()}</p>
{/if}
