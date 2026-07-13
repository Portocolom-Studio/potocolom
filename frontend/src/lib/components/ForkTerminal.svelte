<script lang="ts">
	import { onMount } from 'svelte';
	import { t } from '$lib/i18n.svelte';
	import { cn } from '$lib/utils';

	const stepKeys = [
		{ cmd: 'fork.cmd1' as const, out: 'fork.out1' as const },
		{ cmd: 'fork.cmd2' as const, out: 'fork.out2' as const },
		{ cmd: 'fork.cmd3' as const, out: 'fork.out3' as const }
	] as const;

	type HistoryLine = { kind: 'cmd'; text: string } | { kind: 'out'; text: string };

	let {
		class: className
	}: {
		class?: string;
	} = $props();

	const promptPath = '~/potocolom';

	let history = $state<HistoryLine[]>([]);
	let draft = $state('');
	let streamOut = $state('');
	let cursorOn = $state(true);
	let terminalBody = $state<HTMLDivElement | null>(null);

	$effect(() => {
		history.length;
		draft;
		streamOut;
		const el = terminalBody;
		if (!el) return;
		el.scrollTop = el.scrollHeight;
	});

	onMount(() => {
		let cancelled = false;
		const cursorTimer = setInterval(() => {
			cursorOn = !cursorOn;
		}, 530);

		function sleep(ms: number) {
			return new Promise<void>((resolve) => {
				setTimeout(resolve, ms);
			});
		}

		async function typeText(text: string, ms: number, onChunk: (value: string) => void) {
			let value = '';
			for (const ch of text) {
				if (cancelled) return;
				value += ch;
				onChunk(value);
				await sleep(ms);
			}
		}

		async function run() {
			const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

			if (reduced) {
				for (const step of stepKeys) {
					history.push({ kind: 'cmd', text: t(step.cmd) });
					history.push({ kind: 'out', text: t(step.out) });
				}
				return;
			}

			for (const step of stepKeys) {
				if (cancelled) return;

				await typeText(t(step.cmd), 34, (value) => {
					draft = value;
				});
				if (cancelled) return;

				await sleep(320);
				history.push({ kind: 'cmd', text: draft });
				draft = '';

				streamOut = '';
				await typeText(t(step.out), 14, (value) => {
					streamOut = value;
				});
				if (cancelled) return;

				history.push({ kind: 'out', text: streamOut });
				streamOut = '';
				await sleep(520);
			}
		}

		run();

		return () => {
			cancelled = true;
			clearInterval(cursorTimer);
		};
	});
</script>

<div
	class={cn(
		'border-border bg-[#0a0d12] flex h-full min-h-[22rem] flex-col overflow-hidden rounded-xl border',
		className
	)}
>
	<div class="border-border flex items-center gap-2 border-b px-4 py-2.5">
		<span class="size-2.5 shrink-0 rounded-full bg-[#ff5f57]" aria-hidden="true"></span>
		<span class="size-2.5 shrink-0 rounded-full bg-[#febc2e]" aria-hidden="true"></span>
		<span class="size-2.5 shrink-0 rounded-full bg-[#28c840]" aria-hidden="true"></span>
		<span class="text-muted-foreground mx-auto truncate font-mono text-xs">
			zsh - {promptPath}
		</span>
	</div>

	<div
		bind:this={terminalBody}
		class="text-foreground flex-1 overflow-x-auto overflow-y-auto p-4 font-mono text-[0.8125rem] leading-relaxed"
		aria-live="polite"
		aria-label="Terminal simulation"
	>
		{#each history as line, index (index)}
			{#if line.kind === 'cmd'}
				<div class="terminal-prompt whitespace-nowrap">
					<span class="text-primary shrink-0">{promptPath}</span>
					<span class="text-foreground shrink-0">$</span>
					<span>{line.text}</span>
				</div>
			{:else}
				<div class="text-muted-foreground mt-1 mb-3 break-all whitespace-pre-wrap">{line.text}</div>
			{/if}
		{/each}

		{#if draft}
			<div class="terminal-prompt whitespace-nowrap">
				<span class="text-primary shrink-0">{promptPath}</span>
				<span class="text-foreground shrink-0">$</span>
				<span>{draft}</span><span class="terminal-cursor" class:terminal-cursor-off={!cursorOn}
					>█</span
				>
			</div>
		{:else if streamOut}
			<div class="text-muted-foreground break-all whitespace-pre-wrap">
				{streamOut}<span class="terminal-cursor" class:terminal-cursor-off={!cursorOn}>█</span>
			</div>
		{:else}
			<div class="terminal-prompt whitespace-nowrap">
				<span class="text-primary shrink-0">{promptPath}</span>
				<span class="text-foreground shrink-0">$</span>
				<span class="terminal-cursor" class:terminal-cursor-off={!cursorOn}>█</span>
			</div>
		{/if}
	</div>
</div>

<style>
	.terminal-prompt {
		display: flex;
		flex-wrap: nowrap;
		align-items: baseline;
		column-gap: 0.5rem;
	}

	.terminal-cursor {
		color: var(--foreground);
	}

	.terminal-cursor-off {
		opacity: 0;
	}
</style>
