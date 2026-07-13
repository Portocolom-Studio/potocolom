<script lang="ts">
	// Self-painting hero background: particles drift through a flow field and
	// leave fading strokes, echoing what the product does. Pure canvas, no deps.
	function scene(canvas: HTMLCanvasElement) {
		const ctx = canvas.getContext('2d');
		if (!ctx) return;

		const dpr = Math.min(window.devicePixelRatio || 1, 2);
		let width = 0;
		let height = 0;

		function resize() {
			width = canvas.clientWidth;
			height = canvas.clientHeight;
			canvas.width = Math.max(1, width * dpr);
			canvas.height = Math.max(1, height * dpr);
			ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
			ctx!.fillStyle = '#070b14';
			ctx!.fillRect(0, 0, width, height);
		}
		resize();
		const observer = new ResizeObserver(resize);
		observer.observe(canvas);

		const COUNT = 90;
		const particles = Array.from({ length: COUNT }, () => ({
			x: Math.random() * width,
			y: Math.random() * height,
			hue: 225 + Math.random() * 65, // indigo to violet, occasional cyan below
			speed: 0.6 + Math.random() * 1.2
		}));
		for (const p of particles) if (Math.random() < 0.18) p.hue = 187;

		let time = 0;
		function step() {
			time += 0.0035;
			// translucent wipe leaves slowly fading trails
			ctx!.globalCompositeOperation = 'source-over';
			ctx!.fillStyle = 'rgba(7, 11, 20, 0.045)';
			ctx!.fillRect(0, 0, width, height);
			ctx!.globalCompositeOperation = 'lighter';
			for (const p of particles) {
				const angle =
					Math.sin(p.x * 0.0022 + time) * 2.4 + Math.cos(p.y * 0.0019 - time * 1.3) * 2.4;
				const nx = p.x + Math.cos(angle) * p.speed;
				const ny = p.y + Math.sin(angle) * p.speed;
				ctx!.strokeStyle = `hsla(${p.hue}, 92%, 68%, 0.55)`;
				ctx!.lineWidth = 1.4;
				ctx!.beginPath();
				ctx!.moveTo(p.x, p.y);
				ctx!.lineTo(nx, ny);
				ctx!.stroke();
				p.x = nx;
				p.y = ny;
				if (p.x < -20 || p.x > width + 20 || p.y < -20 || p.y > height + 20) {
					p.x = Math.random() * width;
					p.y = Math.random() * height;
				}
			}
		}

		const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		let frame = 0;
		// warm start: the canvas is already painted on first sight
		for (let i = 0; i < (reducedMotion ? 600 : 300); i += 1) step();
		if (!reducedMotion) {
			const loop = () => {
				step();
				frame = requestAnimationFrame(loop);
			};
			frame = requestAnimationFrame(loop);
		}

		return () => {
			cancelAnimationFrame(frame);
			observer.disconnect();
		};
	}
</script>

<canvas {@attach scene} aria-hidden="true"></canvas>

<style>
	canvas {
		display: block;
		width: 100%;
		height: 100%;
	}
</style>
