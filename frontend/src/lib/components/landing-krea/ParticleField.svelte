<script lang="ts">
	// A quiet field of dots that lights up and swirls around the pointer.
	// Studied from antigravity.google, which does the same thing behind its
	// hero and its two-column call to action.
	type Dot = { x: number; y: number; hx: number; hy: number; lit: number };

	let {
		density = 0.00048,
		radius = 190,
		class: className
	}: {
		/** Dots per square pixel. */
		density?: number;
		/** Pointer influence radius in px. */
		radius?: number;
		class?: string;
	} = $props();

	let canvas: HTMLCanvasElement | null = $state(null);

	function field(node: HTMLCanvasElement) {
		const context = node.getContext('2d');
		if (!context) return;

		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		let dots: Dot[] = [];
		let pointer: { x: number; y: number } | null = null;
		let frame = 0;
		let width = 0;
		let height = 0;

		const seed = (w: number, h: number) => {
			const count = Math.min(1400, Math.round(w * h * density));
			dots = Array.from({ length: count }, () => {
				const x = Math.random() * w;
				const y = Math.random() * h;
				return { x, y, hx: x, hy: y, lit: 0 };
			});
		};

		const styles = getComputedStyle(node);
		const quiet = styles.getPropertyValue('--pf-quiet').trim() || 'rgba(255,255,255,0.22)';
		const accent = styles.getPropertyValue('--pf-accent').trim() || 'rgb(80,140,255)';

		const draw = () => {
			context.clearRect(0, 0, width, height);
			for (const dot of dots) {
				if (pointer) {
					const dx = dot.hx - pointer.x;
					const dy = dot.hy - pointer.y;
					const distance = Math.hypot(dx, dy);
					if (distance < radius) {
						const pull = (1 - distance / radius) ** 2;
						dot.lit = Math.max(dot.lit, pull);
						// Swirl: push outward along the normal, nudged sideways.
						dot.x =
							dot.hx + (dx / (distance || 1)) * pull * 26 - (dy / (distance || 1)) * pull * 16;
						dot.y =
							dot.hy + (dy / (distance || 1)) * pull * 26 + (dx / (distance || 1)) * pull * 16;
					}
				}
				dot.x += (dot.hx - dot.x) * 0.08;
				dot.y += (dot.hy - dot.y) * 0.08;
				dot.lit *= 0.94;

				const size = 1 + dot.lit * 2.2;
				context.fillStyle = dot.lit > 0.02 ? accent : quiet;
				context.globalAlpha = dot.lit > 0.02 ? 0.35 + dot.lit * 0.65 : 1;
				context.beginPath();
				context.ellipse(dot.x, dot.y, size, size, 0, 0, Math.PI * 2);
				context.fill();
			}
			context.globalAlpha = 1;
		};

		const tick = () => {
			draw();
			frame = requestAnimationFrame(tick);
		};

		const resize = () => {
			const rect = node.getBoundingClientRect();
			const ratio = Math.min(window.devicePixelRatio || 1, 2);
			width = rect.width;
			height = rect.height;
			node.width = Math.round(width * ratio);
			node.height = Math.round(height * ratio);
			context.setTransform(ratio, 0, 0, ratio, 0, 0);
			seed(width, height);
			draw();
		};

		const observer = new ResizeObserver(resize);
		observer.observe(node);
		resize();

		// Listen on the window: the copy sits above this canvas, so section-level
		// listeners would never fire where the pointer actually is.
		const onMove = (event: PointerEvent) => {
			const rect = node.getBoundingClientRect();
			const inside =
				event.clientX >= rect.left - radius &&
				event.clientX <= rect.right + radius &&
				event.clientY >= rect.top - radius &&
				event.clientY <= rect.bottom + radius;
			pointer = inside ? { x: event.clientX - rect.left, y: event.clientY - rect.top } : null;
			if (!reduced && !frame && inside) frame = requestAnimationFrame(tick);
		};

		window.addEventListener('pointermove', onMove, { passive: true });

		return () => {
			observer.disconnect();
			window.removeEventListener('pointermove', onMove);
			if (frame) cancelAnimationFrame(frame);
		};
	}
</script>

<canvas
	bind:this={canvas}
	class="particle-field {className ?? ''}"
	{@attach field}
	aria-hidden="true"
></canvas>

<style>
	.particle-field {
		display: block;
		width: 100%;
		height: 100%;
		pointer-events: none;
	}
</style>
