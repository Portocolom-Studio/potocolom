<script lang="ts">
	import { tick } from 'svelte';
	import CopyIcon from '@lucide/svelte/icons/copy';
	import CheckIcon from '@lucide/svelte/icons/check';
	import BrandMark from '$lib/components/brand-mark.svelte';
	import ModeToggle from '$lib/components/landing/ModeToggle.svelte';
	import { iconSvg } from '$lib/docs/icons';
	import '../../../../../landing-tokens.css';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const repoUrl = 'https://github.com/Portocolom-Studio/potocolom';
	const searchIcon = iconSvg('search');
	// The sidebar is a drawer at or below this width. Keep it equal to the CSS breakpoint.
	const phoneQuery = '(max-width: 860px)';
	const focusable = 'a[href], button:not([disabled]), input:not([disabled])';

	let navOpen = $state(false);
	let navEl = $state<HTMLElement | null>(null);
	let menuEl = $state<HTMLButtonElement | null>(null);
	let copyPageLabel = $state('Copy Page');
	let proseEl = $state<HTMLElement | null>(null);
	let searchEl = $state<HTMLInputElement | null>(null);
	let tabsEl = $state<HTMLElement | null>(null);
	let query = $state('');

	const results = $derived.by(() => {
		const q = query.trim().toLowerCase();
		if (q === '') return [];
		return data.searchIndex.filter((item) =>
			[item.title, item.subtitle, item.tabLabel].some((text) => text.toLowerCase().includes(q))
		);
	});

	async function openNav(focusTarget: () => HTMLElement | null | undefined) {
		navOpen = true;
		await tick();
		focusTarget()?.focus();
	}

	// Opening the menu moves focus into it, so keyboard users land on the first link.
	function toggleNav() {
		if (navOpen) closeNav(true);
		else void openNav(() => navEl?.querySelector<HTMLElement>('a'));
	}

	// Escape, the backdrop and the Close button hand focus back to Menu. Links pass nothing,
	// so the page they open takes focus.
	function closeNav(returnFocus = false) {
		navOpen = false;
		if (returnFocus) menuEl?.focus();
	}

	function keepFocusInNav(event: KeyboardEvent) {
		const items = [...(navEl?.querySelectorAll<HTMLElement>(focusable) ?? [])];
		const first = items.at(0);
		const last = items.at(-1);
		if (!first || !last) return;
		const active = document.activeElement;
		if (!navEl?.contains(active)) {
			event.preventDefault();
			(event.shiftKey ? last : first).focus();
		} else if (event.shiftKey && active === first) {
			event.preventDefault();
			last.focus();
		} else if (!event.shiftKey && active === last) {
			event.preventDefault();
			first.focus();
		}
	}

	function flash(set: (label: string) => void, idle: string, label: string) {
		set(label);
		setTimeout(() => set(idle), 1500);
	}

	async function copyPage() {
		const clone = proseEl?.cloneNode(true) as HTMLElement | undefined;
		clone?.querySelectorAll('.codeblock-bar').forEach((bar) => bar.remove());
		const text = `${data.page.title}\n\n${clone?.textContent ?? ''}`.trim();
		try {
			await navigator.clipboard.writeText(text);
			flash((label) => (copyPageLabel = label), 'Copy Page', 'Copied');
		} catch {
			// Clipboard blocked: keep the label as it is.
		}
	}

	// Code copy buttons live inside the prerendered HTML, so one listener on the
	// prose element handles them all. No inline script is needed for this.
	$effect(() => {
		const el = proseEl;
		if (!el) return;
		const onClick = async (event: MouseEvent) => {
			const button = (event.target as HTMLElement).closest<HTMLButtonElement>('.copy-code');
			if (!button) return;
			const code = button.closest('.codeblock')?.querySelector('code')?.textContent ?? '';
			try {
				await navigator.clipboard.writeText(code);
				flash((label) => (button.textContent = label), 'Copy', 'Copied');
			} catch {
				// Clipboard blocked: keep the label as it is.
			}
		};
		el.addEventListener('click', onClick);
		return () => el.removeEventListener('click', onClick);
	});

	// A new page starts with the sidebar closed and an empty search.
	$effect(() => {
		void data.page.tab;
		void data.page.slug;
		query = '';
		navOpen = false;
	});

	// Ctrl K (Cmd K on Mac) moves the cursor to the sidebar search. On a phone the
	// drawer is closed, so it opens first and search gets focus once it is shown.
	$effect(() => {
		const onKey = (event: KeyboardEvent) => {
			// Escape closes the mobile menu, so keyboard users are not stuck in it.
			if (event.key === 'Escape' && navOpen) closeNav(true);
			if (event.key === 'Tab' && navOpen) keepFocusInNav(event);
			if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
				event.preventDefault();
				if (!navOpen && matchMedia(phoneQuery).matches) void openNav(() => searchEl);
				else searchEl?.focus();
			}
		};
		window.addEventListener('keydown', onKey);
		return () => window.removeEventListener('keydown', onKey);
	});

	$effect(() => {
		const media = matchMedia(phoneQuery);
		const onChange = () => {
			if (!media.matches) navOpen = false;
		};
		media.addEventListener('change', onChange);
		return () => media.removeEventListener('change', onChange);
	});

	$effect(() => {
		if (!navOpen) return;
		const previous = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.body.style.overflow = previous;
		};
	});

	// The phone tab row scrolls, so bring the current section tab into view on each page.
	$effect(() => {
		void data.page.tab;
		tabsEl
			?.querySelector<HTMLElement>('[aria-current="true"]')
			?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
	});
	// "On this page" follows the reader: the last heading that has scrolled past the top wins.
	let activeId = $state<string | null>(null);

	$effect(() => {
		const ids = data.toc.map((entry) => entry.id);
		let frame = 0;
		const update = () => {
			frame = 0;
			let current: string | null = null;
			for (const id of ids) {
				const el = document.getElementById(id);
				if (el && el.getBoundingClientRect().top <= 140) current = id;
			}
			activeId = current;
		};
		const schedule = () => {
			if (!frame) frame = requestAnimationFrame(update);
		};
		update();
		window.addEventListener('scroll', schedule, { passive: true });
		window.addEventListener('resize', schedule);
		return () => {
			window.removeEventListener('scroll', schedule);
			window.removeEventListener('resize', schedule);
			if (frame) cancelAnimationFrame(frame);
		};
	});
</script>

<svelte:head>
	<title>{data.page.title} | potocolom docs</title>
	<meta name="description" content={data.page.subtitle} />
	<link rel="canonical" href={data.canonical} />
</svelte:head>

<div class="landing-surface docs" data-docs-shell>
	<a class="skip" href="#article">Skip to content</a>

	<nav id="docs-nav" bind:this={navEl} class="side" class:open={navOpen} aria-label="Documentation">
		<div class="side-top">
			<a class="brand" href="/" aria-label="potocolom home"><BrandMark /></a>
			<div class="side-tools">
				<ModeToggle />
				<button class="close-nav" type="button" onclick={() => closeNav(true)}>Close menu</button>
			</div>
		</div>

		<label class="search">
			<span class="search-ico">{@html searchIcon}</span>
			<input
				bind:this={searchEl}
				bind:value={query}
				type="search"
				name="search"
				placeholder="Search..."
				aria-label="Search the docs"
				autocomplete="off"
				spellcheck="false"
				onkeydown={(event) => {
					if (event.key === 'Escape') query = '';
				}}
			/>
			<kbd>Ctrl&nbsp;K</kbd>
		</label>

		{#if query.trim() !== ''}
			<ul class="results">
				{#each results as item (item.href)}
					<li>
						<a href={item.href} onclick={() => closeNav()}>
							<span class="ico">{@html item.icon}</span>
							<span class="res-text">
								<span class="res-title">{item.title}</span>
								<span class="res-sub">{item.tabLabel}</span>
							</span>
						</a>
					</li>
				{:else}
					<li class="none">No pages match. Try a shorter word.</li>
				{/each}
			</ul>
		{:else}
			{#each data.groups as group (group.label)}
				<div class="group" role="group" aria-label={group.label}>
					<div class="group-title">{group.label}</div>
					<ul>
						{#each group.pages as item (item.slug)}
							<li>
								<a
									href={item.href}
									aria-current={item.current ? 'page' : undefined}
									onclick={() => closeNav()}
								>
									<span class="ico">{@html item.icon}</span>
									<span>{item.title}</span>
								</a>
							</li>
						{/each}
					</ul>
				</div>
			{/each}
		{/if}
	</nav>

	{#if navOpen}
		<button
			class="scrim"
			type="button"
			tabindex="-1"
			aria-hidden="true"
			onclick={() => closeNav(true)}
		></button>
	{/if}

	<div class="main">
		<header class="tabbar">
			<button
				class="menu-btn"
				type="button"
				aria-controls="docs-nav"
				bind:this={menuEl}
				aria-expanded={navOpen}
				onclick={toggleNav}>Menu</button
			>
			<nav class="tabs" bind:this={tabsEl} aria-label="Docs sections">
				{#each data.tabs as tab (tab.slug)}
					<a href={tab.href} aria-current={tab.slug === data.page.tab ? 'true' : undefined}
						>{tab.label}</a
					>
				{/each}
			</nav>
			<a class="top-link" href={repoUrl}>GitHub</a>
		</header>

		<div class="content">
			<main class="article" id="article" tabindex="-1">
				<div class="head-row">
					<h1>{data.page.title}</h1>
					<p class="subtitle">{data.page.subtitle}</p>
					<button class="copy-page" type="button" onclick={copyPage} title={copyPageLabel}>
						{#if copyPageLabel === 'Copied'}
							<CheckIcon aria-hidden="true" />
						{:else}
							<CopyIcon aria-hidden="true" />
						{/if}
						<span class="copy-label" aria-live="polite">{copyPageLabel}</span>
					</button>
				</div>

				<div class="prose" bind:this={proseEl}>
					{@html data.html}
				</div>

				<nav class="pager" aria-label="Previous and next pages">
					{#if data.prev}
						<a href={data.prev.href}>
							<span class="dir">Previous</span>
							<span class="name">{data.prev.title}</span>
						</a>
					{:else}
						<span></span>
					{/if}
					{#if data.next}
						<a class="next" href={data.next.href}>
							<span class="dir">Next</span>
							<span class="name">{data.next.title}</span>
						</a>
					{/if}
				</nav>
			</main>

			<aside class="toc" aria-label="On this page">
				{#if data.toc.length > 0}
					<div class="toc-title">On this page</div>
					<ol>
						{#each data.toc as entry (entry.id)}
							<li>
								<a href="#{entry.id}" aria-current={activeId === entry.id ? 'true' : undefined}
									>{entry.text}</a
								>
							</li>
						{/each}
					</ol>
				{/if}
			</aside>
		</div>
	</div>
</div>

<style>
	/* Docs-only surfaces. Landing, auth and studio keep the shared tokens. Dark values
	   are local; the light selector below is more specific than the landing one. */
	.docs {
		--docs-accent: var(--k-focus);
		--docs-warn: oklch(0.8 0.15 75);
		--docs-planned: oklch(0.75 0.12 300);
		--k-paper: oklch(0.18 0.004 265);
		--k-panel: oklch(0.22 0.004 265);
		--k-hover: oklch(0.26 0.004 265);
		--k-ink: oklch(0.94 0.004 265);
		--k-muted: oklch(0.72 0.008 265);
		--k-line: oklch(1 0 0 / 12%);
		display: grid;
		grid-template-columns: 264px minmax(0, 1fr);
		min-height: 100svh;
		font-family: var(--k-display);
		font-size: 17px;
	}
	:global(:root[data-landing-mode='light']) .docs {
		--docs-warn: oklch(0.5 0.14 65);
		--docs-planned: oklch(0.5 0.16 300);
		--k-paper: oklch(0.985 0.002 265);
		--k-panel: oklch(1 0 0);
		--k-hover: oklch(0.955 0.004 265);
		--k-ink: oklch(0.2 0.01 265);
		--k-muted: oklch(0.42 0.01 265);
		--k-line: oklch(0.2 0.01 265 / 12%);
	}

	/* landing-tokens.css sets white-space: nowrap on every link. Docs links must wrap. */
	.docs :global(a) {
		color: inherit;
		text-decoration: none;
		white-space: normal;
		touch-action: manipulation;
	}
	button,
	input {
		font: inherit;
		color: inherit;
		touch-action: manipulation;
	}
	/* Visible only when keyboard focus reaches it. */
	.skip {
		position: absolute;
		top: 8px;
		left: 8px;
		z-index: 60;
		padding: 8px 12px;
		background: var(--k-panel);
		border: 1px solid var(--k-line);
		border-radius: 8px;
		transform: translateY(-200%);
	}
	.skip:focus {
		transform: none;
	}
	:focus-visible {
		outline: 2px solid var(--k-focus);
		outline-offset: 2px;
		border-radius: 4px;
	}

	/* Sidebar */
	.side {
		position: sticky;
		top: 0;
		align-self: start;
		height: 100svh;
		padding: 22px 20px 32px 24px;
		overflow-y: auto;
		overscroll-behavior: contain;
	}
	.side-top {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 22px;
		padding: 0 4px;
	}
	.side-tools {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.close-nav {
		display: none;
		align-items: center;
		height: 36px;
		padding: 0 12px;
		background: none;
		border: 1px solid var(--k-line);
		border-radius: 8px;
		color: var(--k-muted);
		font-size: 14px;
		cursor: pointer;
	}
	.close-nav:hover {
		color: var(--k-ink);
		background: var(--k-hover);
	}
	.brand {
		font-size: 20px;
		font-weight: 900;
		letter-spacing: -0.02em;
	}
	.search {
		display: flex;
		align-items: center;
		gap: 8px;
		height: 42px;
		margin-bottom: 26px;
		padding: 0 10px 0 12px;
		background: var(--k-panel);
		border: 1px solid var(--k-line);
		border-radius: 10px;
		color: var(--k-muted);
	}
	.search:focus-within {
		border-color: var(--k-focus);
	}
	.search input {
		flex: 1;
		min-width: 0;
		background: none;
		border: 0;
		outline: none;
		color: var(--k-ink);
		font-size: 15px;
	}
	.search input::placeholder {
		color: var(--k-muted);
	}
	.search kbd {
		padding: 0 6px;
		border: 1px solid var(--k-line);
		border-radius: 5px;
		font-family: var(--k-mono);
		font-size: 13px;
	}
	.search-ico :global(.docs-icon) {
		display: block;
		width: 16px;
		height: 16px;
	}

	.group + .group {
		margin-top: 24px;
		padding-top: 20px;
		border-top: 1px solid var(--k-line);
	}
	.group-title {
		margin-bottom: 8px;
		padding: 0 12px;
		color: var(--k-ink);
		font-size: 14px;
		font-weight: 700;
	}
	.group ul,
	.results {
		display: grid;
		gap: 2px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.group a,
	.results a {
		display: flex;
		align-items: center;
		gap: 12px;
		min-height: 38px;
		padding: 8px 12px;
		border-radius: 8px;
		color: var(--k-muted);
		font-size: 15px;
		line-height: 1.35;
	}
	.group a:hover,
	.results a:hover {
		color: var(--k-ink);
		background: var(--k-hover);
	}
	.group a[aria-current='page'] {
		color: var(--docs-accent);
		font-weight: 600;
		background: var(--k-panel);
		box-shadow: inset 2px 0 0 var(--docs-accent);
	}
	.ico {
		display: inline-flex;
		flex: none;
		width: 18px;
		height: 18px;
	}
	.ico :global(.docs-icon) {
		display: block;
		width: 18px;
		height: 18px;
	}
	.res-text {
		display: grid;
		min-width: 0;
	}
	.res-sub {
		color: var(--k-muted);
		font-size: 13px;
	}
	.none {
		padding: 8px 10px;
		color: var(--k-muted);
		font-size: 15px;
	}

	/* Main column: tab bar on top, then the article and "On this page" */
	.main {
		min-width: 0;
	}
	.tabbar {
		position: sticky;
		top: 0;
		z-index: 20;
		display: flex;
		align-items: center;
		gap: 32px;
		height: 60px;
		padding: 0 48px;
		background: var(--k-paper);
		border-bottom: 1px solid var(--k-line);
	}
	.tabs {
		display: flex;
		align-self: stretch;
		gap: 32px;
		overflow-x: auto;
		scrollbar-width: none;
	}
	.tabs a {
		position: relative;
		display: flex;
		align-items: center;
		color: var(--k-muted);
		font-size: 15px;
		white-space: nowrap;
	}
	.tabs a:hover {
		color: var(--k-ink);
	}
	.tabs a[aria-current='true'] {
		color: var(--docs-accent);
		font-weight: 600;
	}
	.tabs a[aria-current='true']::after {
		content: '';
		position: absolute;
		right: 0;
		bottom: -1px;
		left: 0;
		height: 2px;
		background: var(--docs-accent);
	}
	.menu-btn {
		display: none;
		padding: 6px 10px;
		background: none;
		border: 1px solid var(--k-line);
		border-radius: 8px;
		cursor: pointer;
	}
	.top-link {
		margin-left: auto;
		color: var(--k-muted);
		font-size: 15px;
	}
	.top-link:hover {
		color: var(--k-ink);
	}

	.content {
		display: grid;
		grid-template-columns: minmax(0, 70ch) 190px;
		justify-content: center;
		column-gap: 44px;
		padding: 0 40px;
	}
	.article {
		min-width: 0;
		padding: 56px 0 96px;
	}
	.head-row {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		grid-template-areas:
			'title copy'
			'sub sub';
		align-items: start;
		column-gap: 16px;
	}
	h1 {
		grid-area: title;
		margin: 0;
		font-size: 42px;
		font-weight: 700;
		line-height: 1.12;
		letter-spacing: -0.02em;
		overflow-wrap: anywhere;
	}
	.subtitle {
		grid-area: sub;
		margin: 10px 0 0;
		color: var(--k-muted);
		font-size: 19px;
		line-height: 1.5;
	}
	.copy-page {
		grid-area: copy;
		display: inline-flex;
		flex: none;
		align-items: center;
		gap: 8px;
		height: 36px;
		margin-top: 6px;
		padding: 0 12px;
		background: var(--k-panel);
		border: 1px solid var(--k-line);
		border-radius: 8px;
		color: var(--k-muted);
		font-size: 14px;
		white-space: nowrap;
		cursor: pointer;
	}
	.copy-page:hover {
		color: var(--k-ink);
		background: var(--k-hover);
	}
	.copy-page :global(svg) {
		display: block;
		width: 15px;
		height: 15px;
	}

	/* Markdown output. The HTML is injected, so these rules are global under .prose. */
	.prose {
		margin-top: 36px;
		font-size: 17px;
		line-height: 1.7;
	}
	.prose :global(p) {
		margin: 0 0 18px;
	}
	.prose :global(h2) {
		margin: 52px 0 14px;
		font-size: 25px;
		font-weight: 700;
		line-height: 1.25;
		letter-spacing: -0.01em;
		scroll-margin-top: 90px;
	}
	.prose :global(h3) {
		margin: 32px 0 12px;
		font-size: 20px;
		font-weight: 700;
		scroll-margin-top: 90px;
	}
	.prose :global(ul),
	.prose :global(ol) {
		margin: 0 0 18px;
		padding-left: 22px;
	}
	.prose :global(li) {
		margin-bottom: 6px;
	}
	.prose :global(a) {
		color: var(--docs-accent);
		text-decoration: underline;
		text-underline-offset: 3px;
	}
	.prose :global(p code),
	.prose :global(li code),
	.prose :global(td code) {
		padding: 1px 5px;
		background: var(--k-hover);
		border: 1px solid var(--k-line);
		border-radius: 4px;
		font-family: var(--k-mono);
		font-size: 0.9em;
	}
	.prose :global(table) {
		display: block;
		width: 100%;
		margin: 16px 0 22px;
		overflow-x: auto;
		border-collapse: collapse;
		font-size: 15px;
	}
	.prose :global(th),
	.prose :global(td) {
		padding: 10px 12px;
		text-align: left;
		vertical-align: top;
		border-bottom: 1px solid var(--k-line);
	}
	.prose :global(th) {
		font-weight: 700;
	}

	/* Code blocks */
	.prose :global(.codeblock) {
		margin: 18px 0 24px;
		background: var(--k-panel);
		border: 1px solid var(--k-line);
		border-radius: 12px;
		overflow: hidden;
	}
	.prose :global(.codeblock-bar) {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 8px 14px;
		border-bottom: 1px solid var(--k-line);
		color: var(--k-muted);
		font-size: 13px;
	}
	.prose :global(.copy-code) {
		padding: 2px 8px;
		background: none;
		border: 0;
		border-radius: 4px;
		color: var(--k-muted);
		font-size: 13px;
		cursor: pointer;
	}
	.prose :global(.copy-code:hover) {
		color: var(--k-ink);
		background: var(--k-hover);
	}
	.prose :global(pre) {
		margin: 0;
		padding: 14px 16px;
		overflow-x: auto;
		font-family: var(--k-mono);
		font-size: 15px;
		line-height: 1.6;
	}
	.prose :global(pre code) {
		font-family: var(--k-mono);
	}

	/* Callouts */
	.prose :global(.callout) {
		margin: 22px 0;
		padding: 16px 18px;
		background: var(--k-panel);
		border: 1px solid var(--k-line);
		border-left: 3px solid var(--docs-accent);
		border-radius: 10px;
	}
	.prose :global(.callout p:last-child) {
		margin-bottom: 0;
	}
	.prose :global(.callout-label) {
		display: block;
		margin-bottom: 4px;
		color: var(--k-muted);
		font-size: 13px;
		font-weight: 700;
		letter-spacing: 0;
		text-transform: none;
	}
	.prose :global(.callout-planned) {
		border-left-color: var(--docs-planned);
	}
	.prose :global(.callout-planned .callout-label) {
		color: var(--docs-planned);
	}
	.prose :global(.callout-warning) {
		border-left-color: var(--docs-warn);
	}
	.prose :global(.callout-warning .callout-label) {
		color: var(--docs-warn);
	}

	/* Link cards on the Introduction page: one column of compact rows.
	   Grid: icon in column 1, title and text stacked in column 2, arrow in column 3. */
	.prose :global(.docs-cards) {
		display: grid;
		gap: 10px;
		margin: 12px 0 30px;
	}
	.prose :global(.docs-card) {
		position: relative;
		display: grid;
		grid-template-columns: 28px minmax(0, 1fr) 16px;
		column-gap: 16px;
		align-items: center;
		min-height: 84px;
		padding: 16px 18px;
		background: var(--k-panel);
		border: 1px solid var(--k-line);
		border-radius: 12px;
		color: var(--k-ink);
		text-decoration: none;
	}
	.prose :global(.docs-card:hover) {
		background: var(--k-hover);
	}
	.prose :global(.docs-card::after) {
		grid-column: 3;
		grid-row: 1 / span 2;
		justify-self: center;
		width: 8px;
		height: 8px;
		border-top: 2px solid var(--k-muted);
		border-right: 2px solid var(--k-muted);
		transform: rotate(45deg);
		content: '';
	}
	.prose :global(.docs-icon) {
		grid-column: 1;
		grid-row: 1 / span 2;
		display: block;
		width: 28px;
		height: 28px;
		margin: 0;
		color: var(--docs-accent);
	}
	.prose :global(.docs-card-title) {
		grid-column: 2;
		grid-row: 1;
		align-self: end;
		font-size: 18px;
		font-weight: 700;
		line-height: 1.3;
	}
	.prose :global(.docs-card-text) {
		grid-column: 2;
		grid-row: 2;
		align-self: start;
		margin-top: 2px;
		color: var(--k-muted);
		font-size: 15px;
		line-height: 1.5;
	}

	/* Pager */
	.pager {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		margin-top: 72px;
		padding-top: 30px;
		border-top: 1px solid var(--k-line);
	}
	.pager a {
		display: block;
		padding: 14px 16px;
		border: 1px solid var(--k-line);
		border-radius: 10px;
	}
	.pager a:hover {
		background: var(--k-hover);
	}
	.pager a.next {
		grid-column: 2;
		text-align: right;
	}
	.pager .dir {
		display: block;
		color: var(--k-muted);
		font-size: 13px;
	}
	.pager .name {
		font-weight: 700;
	}

	/* On this page */
	.toc {
		position: sticky;
		top: 60px;
		align-self: start;
		padding: 56px 0 0;
		font-size: 15px;
	}
	.toc-title {
		margin-bottom: 10px;
		color: var(--k-ink);
		font-weight: 600;
	}
	.toc ol {
		margin: 0;
		padding: 0;
		list-style: none;
		border-left: 1px solid var(--k-line);
	}
	.toc li a {
		display: block;
		margin-left: -1px;
		padding: 5px 0 5px 14px;
		color: var(--k-muted);
		border-left: 2px solid transparent;
	}
	.toc li a[aria-current='true'] {
		color: var(--docs-accent);
		border-left-color: var(--docs-accent);
	}
	.toc li a:hover {
		color: var(--k-ink);
	}

	.scrim {
		display: none;
	}

	/* Responsive */
	@media (max-width: 1240px) {
		.content {
			grid-template-columns: minmax(0, 70ch);
		}
		.toc {
			display: none;
		}
	}
	/* Phone and tablet: the sidebar becomes a drawer behind a 60px tab row. */
	@media (max-width: 860px) {
		.docs {
			grid-template-columns: minmax(0, 1fr);
		}
		.side {
			position: fixed;
			inset: 0 auto 0 0;
			z-index: 40;
			display: none;
			width: min(300px, 84vw);
			padding: 18px 18px 32px;
			background: var(--k-paper);
			border-right: 1px solid var(--k-line);
		}
		.scrim {
			display: block;
			position: fixed;
			inset: 0;
			z-index: 35;
			border: 0;
			background: oklch(0 0 0 / 50%);
		}
		.side.open {
			display: block;
		}
		.close-nav {
			display: inline-flex;
		}
		.menu-btn {
			display: inline-flex;
			flex: none;
			align-items: center;
			height: 36px;
			padding: 0 14px;
			color: var(--k-ink);
			font-size: 15px;
		}
		.tabbar {
			gap: 12px;
			height: 60px;
			padding: 0 0 0 16px;
		}
		.tabs {
			flex: 1;
			min-width: 0;
			gap: 22px;
			padding-right: 16px;
			overflow-x: auto;
			overflow-y: hidden;
		}
		.top-link {
			display: none;
		}
		.content {
			padding: 0;
		}
		.article {
			padding: 28px 20px 64px;
		}
		h1 {
			font-size: 32px;
		}
		.subtitle {
			font-size: 17px;
		}
		.copy-page {
			margin-top: 4px;
		}
		.pager {
			grid-template-columns: 1fr;
		}
		.pager a.next {
			grid-column: 1;
		}
	}
	@media (max-width: 480px) {
		.copy-page {
			width: 36px;
			justify-content: center;
			padding: 0;
		}
		.copy-label {
			position: absolute;
			width: 1px;
			height: 1px;
			padding: 0;
			margin: -1px;
			overflow: hidden;
			clip-path: inset(50%);
			white-space: nowrap;
		}
	}
	/* Touch screens get no keyboard shortcut hint. */
	@media (pointer: coarse) and (max-width: 860px) {
		.search kbd {
			display: none;
		}
	}
</style>
