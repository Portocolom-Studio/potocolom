<script lang="ts">
	// A quiet field of dots. The pointer lights and swirls them; when `active`
	// flips on they gather onto a pair of glyphs drawn either side of the box,
	// the way antigravity.google wraps a hovered column in braces.
	type Dot = {
		x: number;
		y: number;
		hx: number;
		hy: number;
		tx: number | null;
		ty: number | null;
		lit: number;
	};

	let {
		density = 0.0009,
		radius = 190,
		/** Two characters drawn either side of the box, e.g. "()" or "{}". */
		glyph,
		/** When true the dots gather onto the glyph outline. */
		active = false
	}: {
		density?: number;
		radius?: number;
		glyph?: string;
		active?: boolean;
	} = $props();

	function field(canvas: HTMLCanvasElement) {
		const context = canvas.getContext('2d');
		if (!context) return;

		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		let dots: Dot[] = [];
		let pointer: { x: number; y: number } | null = null;
		let frame = 0;
		let width = 0;
		let height = 0;
		let visible = true;

		const styles = getComputedStyle(canvas);
		const quiet = styles.getPropertyValue('--pf-quiet').trim() || 'rgba(255,255,255,0.18)';
		const accent = styles.getPropertyValue('--pf-accent').trim() || 'rgb(80,140,255)';
		const face = styles.fontFamily || 'sans-serif';

		/** Points along a stroked character, in canvas coordinates. */
		const sampleGlyph = (character: string, centreX: number, box: number) => {
			const scratch = document.createElement('canvas');
			scratch.width = Math.max(8, Math.round(box));
			scratch.height = Math.max(8, Math.round(box));
			const pen = scratch.getContext('2d', { willReadFrequently: true });
			if (!pen) return [];
			pen.strokeStyle = '#fff';
			pen.lineWidth = Math.max(2, box * 0.025);
			pen.textAlign = 'center';
			pen.textBaseline = 'middle';
			pen.font = `${Math.round(box * 0.95)}px ${face}`;
			pen.strokeText(character, scratch.width / 2, scratch.height / 2);
			const pixels = pen.getImageData(0, 0, scratch.width, scratch.height).data;
			const points: { x: number; y: number }[] = [];
			for (let y = 0; y < scratch.height; y += 2) {
				for (let x = 0; x < scratch.width; x += 2) {
					if (pixels[(y * scratch.width + x) * 4 + 3] > 120) {
						points.push({
							x: centreX + (x - scratch.width / 2),
							y: height / 2 + (y - scratch.height / 2)
						});
					}
				}
			}
			return points;
		};

		const assignTargets = () => {
			for (const dot of dots) {
				dot.tx = null;
				dot.ty = null;
			}
			if (!glyph || glyph.length < 2 || width < 320) return;
			const box = Math.min(height * 0.8, width * 0.34);
			const targets = [
				...sampleGlyph(glyph[0], width * 0.17, box),
				...sampleGlyph(glyph[1], width * 0.83, box)
			];
			if (!targets.length) return;
			const pool = dots.slice().sort(() => Math.random() - 0.5);
			// Spread the assigned dots across the WHOLE outline, both characters:
			// walking it one point at a time only ever reached the left glyph.
			const assignable = Math.min(pool.length, 900);
			for (let index = 0; index < assignable; index += 1) {
				const target = targets[Math.floor((index * targets.length) / assignable)];
				pool[index].tx = target.x;
				pool[index].ty = target.y;
			}
		};

		const seed = () => {
			const count = Math.min(1800, Math.round(width * height * density));
			dots = Array.from({ length: count }, () => {
				const x = Math.random() * width;
				const y = Math.random() * height;
				return { x, y, hx: x, hy: y, tx: null, ty: null, lit: 0 };
			});
			assignTargets();
		};

		const draw = () => {
			context.clearRect(0, 0, width, height);
			const gathering = active;

			for (const dot of dots) {
				const onGlyph = gathering && dot.tx !== null && dot.ty !== null;
				const goalX = onGlyph ? (dot.tx as number) : dot.hx;
				const goalY = onGlyph ? (dot.ty as number) : dot.hy;

				if (!onGlyph && pointer) {
					const dx = dot.x - pointer.x;
					const dy = dot.y - pointer.y;
					const distance = Math.hypot(dx, dy) || 1;
					if (distance < radius) {
						const pull = (1 - distance / radius) ** 2;
						dot.lit = Math.max(dot.lit, pull);
						dot.x += (dx / distance) * pull * 7 - (dy / distance) * pull * 5;
						dot.y += (dy / distance) * pull * 7 + (dx / distance) * pull * 5;
					}
				}

				dot.x += (goalX - dot.x) * (onGlyph ? 0.13 : 0.07);
				dot.y += (goalY - dot.y) * (onGlyph ? 0.13 : 0.07);
				dot.lit = onGlyph ? Math.min(1, dot.lit + 0.07) : dot.lit * 0.94;

				const size = 1 + dot.lit * 1.7;
				context.fillStyle = dot.lit > 0.03 ? accent : quiet;
				context.globalAlpha = dot.lit > 0.03 ? 0.4 + dot.lit * 0.6 : 1;
				context.beginPath();
				context.ellipse(dot.x, dot.y, size, size, 0, 0, Math.PI * 2);
				context.fill();
			}
			context.globalAlpha = 1;
		};

		const tick = () => {
			if (visible) draw();
			frame = requestAnimationFrame(tick);
		};

		const resize = () => {
			const rect = canvas.getBoundingClientRect();
			const ratio = Math.min(window.devicePixelRatio || 1, 2);
			width = rect.width;
			height = rect.height;
			canvas.width = Math.round(width * ratio);
			canvas.height = Math.round(height * ratio);
			context.setTransform(ratio, 0, 0, ratio, 0, 0);
			seed();
			draw();
		};

		const sizer = new ResizeObserver(resize);
		sizer.observe(canvas);
		resize();

		// Idle off-screen; a field two sections down should not cost frames.
		const watcher = new IntersectionObserver((entries) => {
			visible = entries[0]?.isIntersecting ?? true;
		});
		watcher.observe(canvas);

		// The copy sits above this canvas, so listen on the window instead.
		const onMove = (event: PointerEvent) => {
			const rect = canvas.getBoundingClientRect();
			const inside =
				event.clientX >= rect.left - radius &&
				event.clientX <= rect.right + radius &&
				event.clientY >= rect.top - radius &&
				event.clientY <= rect.bottom + radius;
			pointer = inside ? { x: event.clientX - rect.left, y: event.clientY - rect.top } : null;
		};

		window.addEventListener('pointermove', onMove, { passive: true });
		if (!reduced) frame = requestAnimationFrame(tick);

		return () => {
			sizer.disconnect();
			watcher.disconnect();
			window.removeEventListener('pointermove', onMove);
			if (frame) cancelAnimationFrame(frame);
		};
	}
</script>

<canvas class="particle-field" {@attach field} aria-hidden="true"></canvas>

<style>
	.particle-field {
		display: block;
		width: 100%;
		height: 100%;
		pointer-events: none;
	}
</style>
