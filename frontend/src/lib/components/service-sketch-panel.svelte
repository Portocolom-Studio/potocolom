<script lang="ts">
	import { t } from '$lib/i18n.svelte';
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import * as Card from '$lib/components/ui/card';
	import { Label } from '$lib/components/ui/label';
	import ParamSliderField from '$lib/components/param-slider-field.svelte';

	let {
		mode
	}: {
		// realtime_canvas left this panel when issue #3 built the live surface;
		// these two remain placeholders until their own issues.
		mode: 'edit_image' | 'image_to_text';
	} = $props();

	let disabledNorm = $state(0.5);

	const title = $derived(t(`app.${mode}.title`));
	const description = $derived(t(`app.${mode}.sub`));
</script>

<div class="no-scrollbar h-full overflow-y-auto">
	<div class="mx-auto flex h-full w-full max-w-6xl flex-col gap-4">
		<div class="flex flex-wrap items-start justify-between gap-3">
			<div>
				<h1 class="text-xl font-semibold">{title}</h1>
				<p class="text-muted-foreground mt-1 max-w-3xl text-sm leading-relaxed">{description}</p>
			</div>
			<Badge variant="outline">{t('app.gen.coming_soon')}</Badge>
		</div>

		<div class="grid min-h-[32rem] flex-1 gap-4 lg:grid-cols-[minmax(18rem,24rem)_1fr]">
			<Card.Root>
				<Card.Header>
					<Card.Title class="text-base">
						{mode === 'edit_image' ? t('app.edit_image.controls') : t('app.image_to_text.controls')}
					</Card.Title>
				</Card.Header>
				<Card.Content class="flex flex-col gap-4">
					<button
						type="button"
						disabled
						class="border-border bg-muted/20 text-muted-foreground grid min-h-36 cursor-not-allowed place-items-center rounded-lg border border-dashed px-6 text-center text-sm"
					>
						{mode === 'edit_image'
							? t('app.edit_image.source_empty')
							: t('app.image_to_text.source_empty')}
					</button>
					<div class="flex flex-col gap-2">
						<Label for="sketch-instruction">
							{mode === 'edit_image'
								? t('app.edit_image.instruction')
								: t('app.image_to_text.question')}
						</Label>
						<textarea
							id="sketch-instruction"
							disabled
							class="border-input bg-muted/20 placeholder:text-muted-foreground min-h-28 w-full cursor-not-allowed resize-none rounded-lg border px-3 py-2 text-sm opacity-60"
							placeholder={mode === 'edit_image'
								? t('app.edit_image.instruction_placeholder')
								: t('app.image_to_text.question_placeholder')}></textarea>
					</div>
					{#if mode === 'edit_image'}
						<ParamSliderField
							id="edit-strength"
							label={t('app.edit_image.strength')}
							bind:norm={disabledNorm}
							minLabel={t('app.edit_image.subtle')}
							maxLabel={t('app.edit_image.strong')}
							valueLabel={t('app.gen.coming_soon')}
							disabled
						/>
					{:else}
						<div class="flex flex-col gap-2">
							<Label for="caption-detail">{t('app.image_to_text.detail')}</Label>
							<select
								id="caption-detail"
								disabled
								class="border-input bg-muted/20 h-9 cursor-not-allowed rounded-lg border px-3 text-sm opacity-60"
							>
								<option>{t('app.image_to_text.detail_balanced')}</option>
							</select>
						</div>
					{/if}
					<Button disabled>{t('app.gen.coming_soon')}</Button>
				</Card.Content>
			</Card.Root>
			<Card.Root class="flex min-h-0 flex-col">
				<Card.Header>
					<Card.Title class="text-base">
						{mode === 'edit_image' ? t('app.edit_image.output') : t('app.image_to_text.output')}
					</Card.Title>
				</Card.Header>
				<Card.Content class="flex min-h-0 flex-1">
					<div
						class="border-border text-muted-foreground grid min-h-72 flex-1 place-items-center rounded-lg border border-dashed px-6 text-center text-sm"
					>
						{mode === 'edit_image'
							? t('app.edit_image.output_empty')
							: t('app.image_to_text.output_empty')}
					</div>
				</Card.Content>
			</Card.Root>
		</div>
	</div>
</div>
