<script lang="ts">
	import { collagePreviewList } from '$lib/collage-variants';

	const host = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
</script>

<svelte:head>
	<title>potocolom - collage compare</title>
</svelte:head>

<div class="bg-background min-h-svh p-4 sm:p-6">
	<header class="mb-6">
		<p class="text-primary text-xs font-semibold tracking-[0.18em] uppercase">
			Landing collage compare
		</p>
		<h1 class="mt-1 text-3xl font-semibold">Masonry directions</h1>
		<p class="text-muted-foreground mt-2 max-w-3xl text-sm leading-relaxed">
			Seven masonry layouts that keep each image at its natural aspect ratio, plus a tighter graph
			layout. Run <code class="text-foreground/80">npm run compare:collages</code> to open dedicated dev
			servers for side-by-side review.
		</p>
	</header>

	<div class="grid grid-cols-1 gap-6 xl:grid-cols-2">
		{#each collagePreviewList as variant (variant.id)}
			<section class="border-border/60 overflow-hidden rounded-xl border">
				<div class="border-border/60 flex items-center justify-between gap-3 border-b px-4 py-3">
					<div>
						<h2 class="font-medium">{variant.title}</h2>
						<p class="text-muted-foreground text-xs">
							/collage-preview/{variant.id} · port {variant.port}
						</p>
					</div>
					<a
						class="text-primary text-sm hover:underline"
						href="/collage-preview/{variant.id}"
						target="_blank"
						rel="noreferrer"
					>
						Open full
					</a>
				</div>
				<iframe
					title={variant.title}
					src="/collage-preview/{variant.id}"
					class="bg-background h-[28rem] w-full"
					loading="lazy"
				></iframe>
			</section>
		{/each}
	</div>

	<section class="border-border/60 mt-8 rounded-xl border p-4">
		<h2 class="font-medium">Dedicated servers</h2>
		<ul class="text-muted-foreground mt-2 space-y-1 text-sm">
			<li>
				<a class="text-primary hover:underline" href="http://{host}:5189/collage-preview">
					http://{host}:5189/collage-preview
				</a>
				<span> (hub)</span>
			</li>
			{#each collagePreviewList as variant (variant.id)}
				<li>
					<a
						class="text-primary hover:underline"
						href="http://{host}:{variant.port}/collage-preview/{variant.id}"
					>
						http://{host}:{variant.port}/collage-preview/{variant.id}
					</a>
				</li>
			{/each}
		</ul>
	</section>
</div>
