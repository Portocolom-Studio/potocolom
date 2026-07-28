export type LatentCanvasApi = {
	setCursor: (x: number | null, y: number | null) => void;
};

export type LatentCanvasOptions = {
	seed?: number;
	warmupFrames?: number;
	animate?: boolean;
	followCursor?: boolean;
	onAttach?: (api: LatentCanvasApi) => void;
	onReady?: () => void;
};

export function createRng(seed: number) {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6d2b79f5) >>> 0;
		let t = Math.imul(state ^ (state >>> 15), state | 1);
		t = (t + Math.imul(t ^ (t >>> 7), t | 61)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

type Particle = {
	x: number;
	y: number;
	hue: number;
	speed: number;
};

type Scheme = {
	clear: string;
	fade: string;
	blend: GlobalCompositeOperation;
	stroke: (hue: number) => string;
};

/* Dark is the default, so every canvas that says nothing keeps the look it had.
   A light page opts in with --latent-scheme: light on or above the canvas.
   Light cannot use additive blending: on paper it saturates straight to white. */
const SCHEMES: Record<'dark' | 'light', Scheme> = {
	dark: {
		clear: '#070b14',
		fade: 'rgba(7, 11, 20, 0.045)',
		blend: 'lighter',
		stroke: (hue) => `hsla(${hue}, 92%, 68%, 0.55)`
	},
	light: {
		clear: '#f5f6f9',
		fade: 'rgba(245, 246, 249, 0.05)',
		blend: 'source-over',
		stroke: (hue) => `hsla(${hue}, 74%, 44%, 0.42)`
	}
};

function readScheme(canvas: HTMLCanvasElement): Scheme {
	const styles = getComputedStyle(canvas);
	const name = styles.getPropertyValue('--latent-scheme').trim();
	const base = name === 'light' ? SCHEMES.light : SCHEMES.dark;
	/* Optional overrides so a section can share the page paper (e.g. waitlist
	   matching the particle stage) without changing the global dark clear. */
	const clear = styles.getPropertyValue('--latent-clear').trim();
	const fade = styles.getPropertyValue('--latent-fade').trim();
	return {
		...base,
		clear: clear || base.clear,
		fade: fade || base.fade
	};
}

export function attachLatentCanvas(canvas: HTMLCanvasElement, options: LatentCanvasOptions = {}) {
	const context = canvas.getContext('2d');
	if (!context) return () => {};
	const brush: CanvasRenderingContext2D = context;

	const rng = options.seed === undefined ? Math.random : createRng(options.seed);
	const dpr = Math.min(window.devicePixelRatio || 1, 2);
	let width = 0;
	let height = 0;
	let particles: Particle[] = [];
	let started = false;
	let running = false;
	let scheme = readScheme(canvas);
	let time = 0;
	let frame = 0;
	let cursorX: number | null = null;
	let cursorY: number | null = null;

	function onPointerMove(event: PointerEvent) {
		const rect = canvas.getBoundingClientRect();
		cursorX = event.clientX - rect.left;
		cursorY = event.clientY - rect.top;
	}

	function onPointerLeave() {
		cursorX = null;
		cursorY = null;
	}

	function setCursor(x: number | null, y: number | null) {
		cursorX = x;
		cursorY = y;
	}

	function applySize(): boolean {
		const nextWidth = canvas.clientWidth;
		const nextHeight = canvas.clientHeight;
		if (nextWidth < 1 || nextHeight < 1) return false;

		width = nextWidth;
		height = nextHeight;
		canvas.width = Math.max(1, Math.round(width * dpr));
		canvas.height = Math.max(1, Math.round(height * dpr));
		brush.setTransform(dpr, 0, 0, dpr, 0, 0);
		return true;
	}

	function initParticles() {
		/* Constant density rather than a constant count, floored at the old 90 so the
		   small preview panels are untouched and only large fields gain particles. */
		const count = Math.min(260, Math.max(90, Math.round((width * height) / 11400)));
		particles = Array.from({ length: count }, () => ({
			x: rng() * width,
			y: rng() * height,
			hue: 225 + rng() * 65,
			speed: 0.6 + rng() * 1.2
		}));
		for (const p of particles) if (rng() < 0.18) p.hue = 187;
	}

	function step() {
		time += 0.0035;
		brush.globalCompositeOperation = 'source-over';
		brush.fillStyle = scheme.fade;
		brush.fillRect(0, 0, width, height);
		brush.globalCompositeOperation = scheme.blend;
		for (const p of particles) {
			const organic =
				Math.sin(p.x * 0.0022 + time) * 2.4 + Math.cos(p.y * 0.0019 - time * 1.3) * 2.4;
			let angle = organic;
			if (options.followCursor && cursorX !== null && cursorY !== null) {
				const toward = Math.atan2(cursorY - p.y, cursorX - p.x);
				const wobble = Math.sin(p.x * 0.01 + p.y * 0.01 + time * 2) * 0.35;
				angle = toward + wobble;
			}
			const nx = p.x + Math.cos(angle) * p.speed;
			const ny = p.y + Math.sin(angle) * p.speed;
			brush.strokeStyle = scheme.stroke(p.hue);
			brush.lineWidth = 1.4;
			brush.beginPath();
			brush.moveTo(p.x, p.y);
			brush.lineTo(nx, ny);
			brush.stroke();
			p.x = nx;
			p.y = ny;
			if (p.x < -20 || p.x > width + 20 || p.y < -20 || p.y > height + 20) {
				p.x = rng() * width;
				p.y = rng() * height;
			}
		}
	}

	/* step() leaves the blend mode set, so the clear has to reset it or it is a no-op. */
	function repaint(warmup: number) {
		brush.globalCompositeOperation = 'source-over';
		brush.fillStyle = scheme.clear;
		brush.fillRect(0, 0, width, height);
		initParticles();
		for (let i = 0; i < warmup; i += 1) step();
	}

	function loop() {
		step();
		frame = requestAnimationFrame(loop);
	}

	function start() {
		if (started || !applySize()) return;
		started = true;

		const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		repaint(options.warmupFrames ?? (reducedMotion ? 600 : 300));

		if (!options.animate) {
			observer.disconnect();
		}

		options.onReady?.();

		running = options.animate ?? !reducedMotion;
		if (running) frame = requestAnimationFrame(loop);
	}

	function resize() {
		if (!started) {
			start();
			return;
		}

		if (!applySize()) return;
		time = 0;
		repaint(60);
	}

	/* A fixed full-page field would otherwise keep burning frames in a background tab. */
	function onVisibility() {
		if (document.hidden) {
			cancelAnimationFrame(frame);
			frame = 0;
		} else if (running && !frame) {
			frame = requestAnimationFrame(loop);
		}
	}

	if (options.followCursor && options.onAttach) {
		options.onAttach({ setCursor });
	} else if (options.followCursor) {
		canvas.addEventListener('pointermove', onPointerMove);
		canvas.addEventListener('pointerleave', onPointerLeave);
	}

	const observer = new ResizeObserver(() => resize());
	observer.observe(canvas);

	/* The theme toggle flips an attribute on <html>; re-read the scheme and redraw. */
	const themeObserver = new MutationObserver(() => {
		const next = readScheme(canvas);
		if (next.clear === scheme.clear && next.fade === scheme.fade && next.blend === scheme.blend) {
			return;
		}
		scheme = next;
		if (started) repaint(90);
	});
	themeObserver.observe(document.documentElement, {
		attributes: true,
		attributeFilter: ['data-krea-mode']
	});

	document.addEventListener('visibilitychange', onVisibility);
	requestAnimationFrame(() => start());

	return () => {
		cancelAnimationFrame(frame);
		observer.disconnect();
		themeObserver.disconnect();
		document.removeEventListener('visibilitychange', onVisibility);
		if (options.followCursor) {
			canvas.removeEventListener('pointermove', onPointerMove);
			canvas.removeEventListener('pointerleave', onPointerLeave);
		}
	};
}
