<script lang="ts">
	import { Label } from '$lib/components/ui/label';
	import { Slider } from '$lib/components/ui/slider';

	let {
		id,
		label,
		norm = $bindable(0),
		minLabel,
		maxLabel,
		valueLabel,
		disabled = false
	}: {
		id: string;
		label: string;
		norm?: number;
		minLabel: string;
		maxLabel: string;
		valueLabel: string;
		disabled?: boolean;
	} = $props();

	// Slider track is always 0-100 so bar length stays constant across models.
	const sliderPosition = $derived(Math.round(Math.min(1, Math.max(0, norm)) * 100));

	function onValueChange(value: number): void {
		norm = value / 100;
	}
</script>

<div class="flex flex-col gap-2">
	<div class="flex items-center justify-between gap-2">
		<Label for={id}>{label}</Label>
		<span class="text-muted-foreground text-xs tabular-nums">{valueLabel}</span>
	</div>
	<Slider
		{id}
		type="single"
		min={0}
		max={100}
		step={1}
		{disabled}
		value={sliderPosition}
		onValueChange={onValueChange}
		class="w-full"
	/>
	<div class="text-muted-foreground flex justify-between text-xs tabular-nums">
		<span>{minLabel}</span>
		<span>{maxLabel}</span>
	</div>
</div>
