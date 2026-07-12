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

function createRng(seed: number) {
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
		particles = Array.from({ length: 90 }, () => ({
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
		brush.fillStyle = 'rgba(7, 11, 20, 0.045)';
		brush.fillRect(0, 0, width, height);
		brush.globalCompositeOperation = 'lighter';
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
			brush.strokeStyle = `hsla(${p.hue}, 92%, 68%, 0.55)`;
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

	function start() {
		if (started || !applySize()) return;
		started = true;

		brush.fillStyle = '#070b14';
		brush.fillRect(0, 0, width, height);
		initParticles();

		const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		const warmup = options.warmupFrames ?? (reducedMotion ? 600 : 300);
		for (let i = 0; i < warmup; i += 1) step();

		if (!options.animate) {
			observer.disconnect();
		}

		options.onReady?.();

		const animate = options.animate ?? !reducedMotion;
		if (animate) {
			const loop = () => {
				step();
				frame = requestAnimationFrame(loop);
			};
			frame = requestAnimationFrame(loop);
		}
	}

	function resize() {
		if (!started) {
			start();
			return;
		}

		if (!applySize()) return;

		brush.fillStyle = '#070b14';
		brush.fillRect(0, 0, width, height);
		time = 0;
		initParticles();
		for (let i = 0; i < 60; i += 1) step();
	}

	if (options.followCursor && options.onAttach) {
		options.onAttach({ setCursor });
	} else if (options.followCursor) {
		canvas.addEventListener('pointermove', onPointerMove);
		canvas.addEventListener('pointerleave', onPointerLeave);
	}

	const observer = new ResizeObserver(() => resize());
	observer.observe(canvas);
	requestAnimationFrame(() => start());

	return () => {
		cancelAnimationFrame(frame);
		observer.disconnect();
		if (options.followCursor) {
			canvas.removeEventListener('pointermove', onPointerMove);
			canvas.removeEventListener('pointerleave', onPointerLeave);
		}
	};
}
