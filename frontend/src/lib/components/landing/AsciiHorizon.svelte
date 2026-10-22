<script lang="ts">
	// A field of glyphs in the ASCII style of the sunrise plate: a horizon that
	// rises from the bottom edge and brightens under the pointer. The particles
	// variant adds drifting specks that turn into glyphs near the pointer. Each
	// row is one fillText, and the frame loop steps at about 12 fps on purpose.
	let {
		variant = 'horizon',
		glyphs = '()'
	}: {
		variant?: 'horizon' | 'particles';
		/** Characters the particles show when the pointer is near them. */
		glyphs?: string;
	} = $props();

	const RAMP = ' .-=+xX80S#@';
	const ROW = 15;
	const STEP_MS = 80;
	const FONT = "'JetBrains Mono', ui-monospace, monospace";
	const FALLBACK_COLOURS = [
		'#ffffff',
		'#eef3ff',
		'#a9c0ff',
		'#6a8bff',
		'#4560e0',
		'#3a4a8f',
		'#4b5785'
	];
	const STOPS = [0, 0.42, 0.56, 0.68, 0.8, 0.92, 1];
	const SHAPE = {
		horizon: { cy: 1.16, radius: 0.95, gain: 1 },
		particles: { cy: 1.3, radius: 0.8, gain: 0.7 }
	};

	/** Stable pseudo-random value in [0, 1) for a cell, a particle or a flicker step. */
	function hash(a: number, b: number, c: number): number {
		const x = Math.sin(a * 127.1 + b * 311.7 + c * 74.7) * 43758.5453;
		return x - Math.floor(x);
	}

	function fract(x: number): number {
		return x - Math.floor(x);
	}

	function clamp01(x: number): number {
		return x < 0 ? 0 : x > 1 ? 1 : x;
	}

	function field(canvas: HTMLCanvasElement) {
		const context = canvas.getContext('2d');
		if (!context) return;

		// The pointer listens on the wrapper, so the overlay above can stay
		// pointer-transparent without losing it.
		const host = canvas.parentElement ?? canvas;
		const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		let width = 0;
		let height = 0;
		let cols = 0;
		let rows = 0;
		let cells: string[] = [];
		let rowChars: string[] = [];
		let colours = FALLBACK_COLOURS;
		let t = 0;
		let last = Number.NEGATIVE_INFINITY;
		let frame = 0;
		let visible = true;
		let pointer: { x: number; y: number } | null = null;

		const readColours = () => {
			const styles = getComputedStyle(canvas);
			colours = FALLBACK_COLOURS.map(
				(fallback, index) => styles.getPropertyValue(`--ah-${index}`).trim() || fallback
			);
		};

		const paint = () => {
			const { cy, radius, gain } = variant === 'particles' ? SHAPE.particles : SHAPE.horizon;
			const aspect = width / height;
			const flicker = Math.floor(t * 3);

			for (let j = 0; j < rows; j += 1) {
				const ny = j / rows;
				for (let i = 0; i < cols; i += 1) {
					const nx = i / cols;
					let v = (radius - Math.hypot((nx - 0.5) * aspect, ny - cy)) / 0.6;
					if (v > -0.3) {
						v +=
							0.13 * Math.sin(nx * 13 + t * 1.1) * Math.sin(ny * 7 - t * 0.7) +
							0.07 * Math.sin((nx + ny) * 23 - t * 1.9);
					}
					v *= gain;
					if (pointer) {
						const qx = (nx - pointer.x) * aspect;
						const qy = ny - pointer.y;
						v += 0.55 * Math.exp(-(qx * qx + qy * qy) / 0.006);
					}
					const h = hash(i, j, flicker);
					if (v < 0.75 ? h < 0.18 : h < 0.07) v = 0;
					cells[j * cols + i] = RAMP[Math.round(clamp01(v) * 11)];
				}
			}

			if (variant === 'particles') {
				const glyphSet = glyphs || '()';
				const count = Math.round(cols * rows * 0.03);
				for (let k = 0; k < count; k += 1) {
					const x0 = hash(k, 1, 0);
					const y0 = hash(k, 2, 0);
					const vx = (hash(k, 3, 0) - 0.5) * 0.02;
					const vy = -0.004 - hash(k, 4, 0) * 0.01;
					const x = fract(x0 + vx * t);
					const y = fract(y0 + vy * t + 0.01 * Math.sin(t + k));
					const column = Math.min(cols - 1, Math.floor(x * cols));
					const row = Math.min(rows - 1, Math.floor(y * rows));
					const index = row * cols + column;
					if (cells[index] !== ' ' && cells[index] !== '.') continue;
					let near = false;
					if (pointer) {
						const dx = (x - pointer.x) * aspect;
						const dy = y - pointer.y;
						near = Math.exp(-(dx * dx + dy * dy) / 0.01) > 0.35;
					}
					cells[index] = near ? glyphSet[k % glyphSet.length] : hash(k, 5, 0) < 0.5 ? '.' : ':';
				}
			}

			context.clearRect(0, 0, width, height);
			const glow = context.createRadialGradient(
				width / 2,
				cy * height,
				0,
				width / 2,
				cy * height,
				radius * height
			);
			for (let stop = 0; stop < STOPS.length; stop += 1) {
				glow.addColorStop(STOPS[stop], colours[stop]);
			}
			context.fillStyle = glow;
			for (let j = 0; j < rows; j += 1) {
				for (let i = 0; i < cols; i += 1) rowChars[i] = cells[j * cols + i];
				context.fillText(rowChars.join(''), 0, j * ROW);
			}
		};

		const tick = (now: number) => {
			frame = requestAnimationFrame(tick);
			if (now - last < STEP_MS) return;
			last = now;
			t += 0.08;
			paint();
		};

		// Run only while the field is on screen, the tab is showing and motion
		// is allowed; otherwise hold the last frame.
		const sync = () => {
			const running = !reduced && visible && !document.hidden;
			if (running && !frame) {
				frame = requestAnimationFrame(tick);
			} else if (!running && frame) {
				cancelAnimationFrame(frame);
				frame = 0;
			}
		};

		const resize = () => {
			const rect = canvas.getBoundingClientRect();
			const ratio = Math.min(window.devicePixelRatio || 1, 2);
			width = rect.width;
			height = rect.height;
			canvas.width = Math.round(width * ratio);
			canvas.height = Math.round(height * ratio);
			context.setTransform(ratio, 0, 0, ratio, 0, 0);
			context.font = `13px ${FONT}`;
			context.textBaseline = 'top';
			const cellWidth = context.measureText('M').width || 8;
			cols = Math.floor(width / cellWidth);
			rows = Math.floor(height / ROW);
			cells = new Array<string>(cols * rows).fill(' ');
			rowChars = new Array<string>(cols).fill(' ');
			readColours();
			paint();
		};

		const onMove = (event: PointerEvent) => {
			const rect = canvas.getBoundingClientRect();
			const x = (event.clientX - rect.left) / rect.width;
			const y = (event.clientY - rect.top) / rect.height;
			pointer = x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
		};
		const onLeave = () => {
			pointer = null;
		};

		if (!reduced) {
			host.addEventListener('pointermove', onMove, { passive: true });
			host.addEventListener('pointerleave', onLeave);
		}

		const sizer = new ResizeObserver(resize);
		sizer.observe(canvas);
		const watcher = new IntersectionObserver((entries) => {
			visible = entries[0]?.isIntersecting ?? true;
			sync();
		});
		watcher.observe(canvas);
		document.addEventListener('visibilitychange', sync);
		resize();
		sync();

		return () => {
			sizer.disconnect();
			watcher.disconnect();
			document.removeEventListener('visibilitychange', sync);
			host.removeEventListener('pointermove', onMove);
			host.removeEventListener('pointerleave', onLeave);
			if (frame) cancelAnimationFrame(frame);
			frame = 0;
		};
	}
</script>

<div class="ascii-horizon" class:particles={variant === 'particles'} aria-hidden="true">
	<div class="sunrise"></div>
	<div class="veil"></div>
	<canvas {@attach field}></canvas>
</div>

<style>
	.ascii-horizon {
		position: relative;
		overflow: hidden;
		inline-size: 100%;
		block-size: 100%;
		background: #070b16;
	}

	.sunrise {
		position: absolute;
		inset-inline: 0;
		block-size: 100%;
		inset-block-end: -30%;
		background: url('/images/horizon/sunrise.webp') center bottom / cover no-repeat;
		filter: hue-rotate(195deg) saturate(1.1);
		opacity: 0.55;
	}

	.ascii-horizon.particles .sunrise {
		inset-block-end: -48%;
		opacity: 0.4;
	}

	.veil {
		position: absolute;
		inset: 0;
		background: linear-gradient(180deg, #0a1022 0%, rgb(10 16 34 / 60%) 40%, transparent 70%);
	}

	canvas {
		position: absolute;
		inset: 0;
		display: block;
		inline-size: 100%;
		block-size: 100%;
	}
</style>
