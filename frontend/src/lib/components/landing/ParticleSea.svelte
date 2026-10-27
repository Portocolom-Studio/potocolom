<script lang="ts">
	import { untrack } from 'svelte';

	// A sea of particles under a blue sunrise, shared by the landing footer and
	// the sign-in and join pages. A grid of particles rolls with two octaves of
	// 3D Perlin noise and is seen from a low camera, so it recedes to a horizon;
	// rows near the horizon sit in the glow and are lit from behind. The pointer
	// leans the camera and drops ripples where it meets the water. WebGL2
	// computes the waves in the vertex shader; without it a Canvas 2D version
	// draws a coarser grid at 30 fps. The glow and the fade into the page are
	// CSS, so they still show when neither canvas can start.
	let {
		variant,
		horizonAt
	}: {
		/** `footer` sets the horizon above `horizonAt` and draws brighter;
		 * `field` sets it at a fixed share of the height. */
		variant: 'footer' | 'field';
		/** Element whose top sits just under the horizon (the footer wordmark). */
		horizonAt?: HTMLElement;
	} = $props();

	type Tone = { base: number; range: number; back: number; near: number; rgb: number[] };
	type Camera = {
		width: number;
		height: number;
		ratio: number;
		horizon: number;
		scale: number;
		yaw: number;
	};
	type Ripple = { x: number; z: number; t: number };
	type Engine = {
		resize(camera: Camera): void;
		frame(t: number, camera: Camera, ripples: Ripple[]): void;
		destroy(): void;
	};

	const TONES: Record<'footer' | 'field', Tone> = {
		footer: { base: 0.14, range: 0.7, back: 0.7, near: 0.5, rgb: [30, 210, 60, 185] },
		field: { base: 0.1, range: 0.6, back: 0.6, near: 0.55, rgb: [40, 210, 70, 180] }
	};
	const PITCH = 0.2;
	const RIPPLES = 8;
	const RIPPLE_LIFE = 3;

	/** Phones, tablets and small machines draw a coarser grid. */
	function lowEnd(): boolean {
		const nav = navigator as Navigator & { deviceMemory?: number };
		return (
			window.matchMedia('(pointer: coarse)').matches ||
			(nav.hardwareConcurrency ?? 4) <= 4 ||
			(nav.deviceMemory ?? 8) <= 4
		);
	}

	function seeded(seed: number) {
		return () => (seed = (seed * 16807) % 2147483647) / 2147483647;
	}

	/** The permutation table of a seeded improved Perlin noise. Both engines
	 * use the same one, so the waves match. */
	function permutation(): Uint8Array {
		const random = seeded(5);
		const perm = new Uint8Array(512);
		for (let i = 0; i < 256; i += 1) perm[i] = i;
		for (let i = 255; i > 0; i -= 1) {
			const j = Math.floor(random() * (i + 1));
			[perm[i], perm[j]] = [perm[j], perm[i]];
		}
		for (let i = 0; i < 256; i += 1) perm[i + 256] = perm[i];
		return perm;
	}

	/** The particle grid on the water, in sea units: x across, z away. */
	function grid(cols: number, rows: number): Float32Array {
		const random = seeded(5);
		const points = new Float32Array(cols * rows * 2);
		for (let r = 0; r < rows; r += 1) {
			for (let c = 0; c < cols; c += 1) {
				const i = (r * cols + c) * 2;
				points[i] = (c / (cols - 1) - 0.5) * 9 + (random() - 0.5) * 0.03;
				points[i + 1] = (r / (rows - 1)) * 7 + 0.3 + (random() - 0.5) * 0.03;
			}
		}
		return points;
	}

	const DRAW_VS = `#version 300 es
precision highp float;
precision highp int;
in vec2 aGrid;
uniform highp usampler2D uPerm;
uniform float uT; uniform float uYaw; uniform float uPitch; uniform float uS;
uniform vec2 uCentre; uniform vec2 uRes; uniform float uDpr;
uniform vec4 uRipple[${RIPPLES}];
uniform vec4 uTone; uniform vec4 uRgb;
out vec4 vCol;
int P(int i) { return int(texelFetch(uPerm, ivec2(i & 511, 0), 0).r); }
float grad(int h, float x, float y, float z) {
  int u = h & 15;
  float a = u < 8 ? x : y;
  float b = u < 4 ? y : (u == 12 || u == 14 ? x : z);
  return ((u & 1) != 0 ? -a : a) + ((u & 2) != 0 ? -b : b);
}
float fade(float t) { return t * t * t * (t * (t * 6.0 - 15.0) + 10.0); }
float noise(vec3 p) {
  vec3 f = floor(p);
  int X = int(f.x) & 255, Y = int(f.y) & 255, Z = int(f.z) & 255;
  p -= f;
  float u = fade(p.x), v = fade(p.y), w = fade(p.z);
  int A = P(X) + Y, AA = P(A) + Z, AB = P(A + 1) + Z, B = P(X + 1) + Y, BA = P(B) + Z, BB = P(B + 1) + Z;
  return mix(
    mix(mix(grad(P(AA), p.x, p.y, p.z), grad(P(BA), p.x - 1.0, p.y, p.z), u),
        mix(grad(P(AB), p.x, p.y - 1.0, p.z), grad(P(BB), p.x - 1.0, p.y - 1.0, p.z), u), v),
    mix(mix(grad(P(AA + 1), p.x, p.y, p.z - 1.0), grad(P(BA + 1), p.x - 1.0, p.y, p.z - 1.0), u),
        mix(grad(P(AB + 1), p.x, p.y - 1.0, p.z - 1.0), grad(P(BB + 1), p.x - 1.0, p.y - 1.0, p.z - 1.0), u), v),
    w);
}
void main() {
  float x = aGrid.x, z = aGrid.y;
  float y = noise(vec3(x * 0.4 + uT * 0.1, z * 0.4 - uT * 0.22, uT * 0.06)) * 0.8
    + noise(vec3(x * 1.2, z * 1.2 + uT * 0.3, 3.3)) * 0.16;
  for (int i = 0; i < ${RIPPLES}; i++) {
    vec4 r = uRipple[i];
    if (r.w < 0.5) continue;
    float age = uT - r.z, d = distance(vec2(x, z), r.xy), front = d - age * 1.6;
    if (front > -1.2 && front < 0.3) y += sin(front * 9.0) * 0.12 * exp(-age * 1.2) * exp(-d * 0.4);
  }
  float cy = cos(uYaw), sy = sin(uYaw), cp = cos(uPitch), sp = sin(uPitch);
  float xr = x * cy + z * sy, zr = -x * sy + z * cy;
  float yc = y - 1.35, y2 = yc * cp - zr * sp, z2 = yc * sp + zr * cp;
  if (z2 < 0.2) { gl_Position = vec4(2.0, 2.0, 0.0, 1.0); gl_PointSize = 0.0; vCol = vec4(0.0); return; }
  vec2 P2 = vec2(uCentre.x + xr * uS / z2, uCentre.y - y2 * uS / z2);
  float near = clamp(1.0 - (z2 - 0.3) / 7.0, 0.0, 1.0);
  float backlit = pow(1.0 - near, 2.2) * (uTone.z + y * 0.6) + max(0.0, y) * 0.25;
  float light = clamp(near * uTone.w + backlit, 0.0, 1.0);
  // The Canvas 2D version sorts particles into twelve colour buckets; match it.
  light = min(floor(light * 12.0), 11.0) / 11.0;
  gl_Position = vec4(P2.x / uRes.x * 2.0 - 1.0, 1.0 - P2.y / uRes.y * 2.0, 0.0, 1.0);
  gl_PointSize = max(1.0, (0.6 + near * 1.9) * uDpr);
  vCol = vec4((uRgb.x + light * uRgb.y) / 255.0, (uRgb.z + light * uRgb.w) / 255.0, 1.0, uTone.x + light * uTone.y);
}`;
	const DRAW_FS = `#version 300 es
precision mediump float;
in vec4 vCol;
out vec4 o;
void main() { o = vec4(vCol.rgb * vCol.a, vCol.a); }`;

	function gpuEngine(canvas: HTMLCanvasElement, tone: Tone): Engine | null {
		const gl = canvas.getContext('webgl2', { antialias: false, premultipliedAlpha: true });
		if (!gl) return null;

		const shaders: WebGLShader[] = [];
		let program: WebGLProgram | null = null;
		let buffer: WebGLBuffer | null = null;
		let vao: WebGLVertexArrayObject | null = null;
		let texture: WebGLTexture | null = null;
		const release = () => {
			if (vao) gl.deleteVertexArray(vao);
			if (buffer) gl.deleteBuffer(buffer);
			if (texture) gl.deleteTexture(texture);
			if (program) gl.deleteProgram(program);
			for (const shader of shaders) gl.deleteShader(shader);
			gl.getExtension('WEBGL_lose_context')?.loseContext();
		};
		const compile = (type: number, source: string) => {
			const shader = gl.createShader(type);
			if (!shader) throw new Error('shader');
			shaders.push(shader);
			gl.shaderSource(shader, source);
			gl.compileShader(shader);
			if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
				throw new Error(gl.getShaderInfoLog(shader) ?? 'shader');
			}
			return shader;
		};

		try {
			program = gl.createProgram();
			if (!program) throw new Error('program');
			gl.attachShader(program, compile(gl.VERTEX_SHADER, DRAW_VS));
			gl.attachShader(program, compile(gl.FRAGMENT_SHADER, DRAW_FS));
			gl.bindAttribLocation(program, 0, 'aGrid');
			gl.linkProgram(program);
			if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
				throw new Error(gl.getProgramInfoLog(program) ?? 'link');
			}
			const u = (name: string) => gl.getUniformLocation(program as WebGLProgram, name);
			const uniforms = {
				perm: u('uPerm'),
				t: u('uT'),
				yaw: u('uYaw'),
				pitch: u('uPitch'),
				s: u('uS'),
				centre: u('uCentre'),
				res: u('uRes'),
				dpr: u('uDpr'),
				ripple: u('uRipple'),
				tone: u('uTone'),
				rgb: u('uRgb')
			};

			const [cols, rows] = lowEnd() ? [160, 90] : [230, 124];
			const count = cols * rows;
			buffer = gl.createBuffer();
			vao = gl.createVertexArray();
			if (!buffer || !vao) throw new Error('buffer');
			gl.bindVertexArray(vao);
			gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
			gl.bufferData(gl.ARRAY_BUFFER, grid(cols, rows), gl.STATIC_DRAW);
			gl.enableVertexAttribArray(0);
			gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
			gl.bindVertexArray(null);
			gl.bindBuffer(gl.ARRAY_BUFFER, null);

			texture = gl.createTexture();
			if (!texture) throw new Error('texture');
			gl.activeTexture(gl.TEXTURE0);
			gl.bindTexture(gl.TEXTURE_2D, texture);
			gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
			gl.texImage2D(
				gl.TEXTURE_2D,
				0,
				gl.R8UI,
				512,
				1,
				0,
				gl.RED_INTEGER,
				gl.UNSIGNED_BYTE,
				permutation()
			);
			gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
			gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);

			const slots = new Float32Array(RIPPLES * 4);
			gl.useProgram(program);
			gl.uniform1i(uniforms.perm, 0);
			gl.uniform1f(uniforms.pitch, PITCH);
			gl.uniform4f(uniforms.tone, tone.base, tone.range, tone.back, tone.near);
			gl.uniform4f(uniforms.rgb, tone.rgb[0], tone.rgb[1], tone.rgb[2], tone.rgb[3]);

			return {
				resize(camera) {
					canvas.width = Math.max(1, Math.round(camera.width * camera.ratio));
					canvas.height = Math.max(1, Math.round(camera.height * camera.ratio));
				},
				frame(t, camera, ripples) {
					if (gl.isContextLost()) return;
					slots.fill(0);
					ripples.slice(-RIPPLES).forEach((r, i) => slots.set([r.x, r.z, r.t, 1], i * 4));
					gl.viewport(0, 0, canvas.width, canvas.height);
					gl.clearColor(0, 0, 0, 0);
					gl.clear(gl.COLOR_BUFFER_BIT);
					gl.enable(gl.BLEND);
					gl.blendFunc(gl.ONE, gl.ONE);
					gl.useProgram(program);
					gl.activeTexture(gl.TEXTURE0);
					gl.bindTexture(gl.TEXTURE_2D, texture);
					gl.uniform1f(uniforms.t, t);
					gl.uniform1f(uniforms.yaw, camera.yaw);
					gl.uniform1f(uniforms.s, camera.scale);
					gl.uniform2f(uniforms.centre, camera.width / 2, camera.horizon);
					gl.uniform2f(uniforms.res, camera.width, camera.height);
					gl.uniform1f(uniforms.dpr, camera.ratio);
					gl.uniform4fv(uniforms.ripple, slots);
					gl.bindVertexArray(vao);
					gl.drawArrays(gl.POINTS, 0, count);
					gl.bindVertexArray(null);
				},
				destroy: release
			};
		} catch {
			release();
			return null;
		}
	}

	function canvasEngine(canvas: HTMLCanvasElement, tone: Tone): Engine | null {
		const context = canvas.getContext('2d');
		if (!context) return null;
		const ctx = context;
		const BUCKETS = 12;
		const colours: string[] = [];
		for (let j = 0; j < BUCKETS; j += 1) {
			const t = j / (BUCKETS - 1);
			const [r0, r1, g0, g1] = tone.rgb;
			colours.push(
				`rgba(${Math.round(r0 + t * r1)},${Math.round(g0 + t * g1)},255,${(tone.base + t * tone.range).toFixed(3)})`
			);
		}
		const perm = permutation();
		const grad = (h: number, x: number, y: number, z: number) => {
			const u = h & 15;
			const a = u < 8 ? x : y;
			const b = u < 4 ? y : u === 12 || u === 14 ? x : z;
			return (u & 1 ? -a : a) + (u & 2 ? -b : b);
		};
		const fade = (t: number) => t * t * t * (t * (t * 6 - 15) + 10);
		const lerp = (a: number, b: number, t: number) => a + t * (b - a);
		const noise = (x: number, y: number, z: number) => {
			const X = Math.floor(x) & 255;
			const Y = Math.floor(y) & 255;
			const Z = Math.floor(z) & 255;
			x -= Math.floor(x);
			y -= Math.floor(y);
			z -= Math.floor(z);
			const u = fade(x);
			const v = fade(y);
			const w = fade(z);
			const A = perm[X] + Y;
			const AA = perm[A] + Z;
			const AB = perm[A + 1] + Z;
			const B = perm[X + 1] + Y;
			const BA = perm[B] + Z;
			const BB = perm[B + 1] + Z;
			return lerp(
				lerp(
					lerp(grad(perm[AA], x, y, z), grad(perm[BA], x - 1, y, z), u),
					lerp(grad(perm[AB], x, y - 1, z), grad(perm[BB], x - 1, y - 1, z), u),
					v
				),
				lerp(
					lerp(grad(perm[AA + 1], x, y, z - 1), grad(perm[BA + 1], x - 1, y, z - 1), u),
					lerp(grad(perm[AB + 1], x, y - 1, z - 1), grad(perm[BB + 1], x - 1, y - 1, z - 1), u),
					v
				),
				w
			);
		};
		const [cols, rows] = lowEnd() ? [110, 60] : [160, 86];
		const points = grid(cols, rows);
		const count = cols * rows;
		const paths: number[][] = Array.from({ length: BUCKETS }, () => []);

		return {
			resize(camera) {
				canvas.width = Math.max(1, Math.round(camera.width * camera.ratio));
				canvas.height = Math.max(1, Math.round(camera.height * camera.ratio));
				ctx.setTransform(camera.ratio, 0, 0, camera.ratio, 0, 0);
			},
			frame(t, camera, ripples) {
				const { width, height, scale, horizon } = camera;
				ctx.clearRect(0, 0, width, height);
				const cy = Math.cos(camera.yaw);
				const sy = Math.sin(camera.yaw);
				const cp = Math.cos(PITCH);
				const sp = Math.sin(PITCH);
				const cx0 = width / 2;
				for (const path of paths) path.length = 0;
				for (let i = 0; i < count; i += 1) {
					const x = points[i * 2];
					const z = points[i * 2 + 1];
					let y =
						noise(x * 0.4 + t * 0.1, z * 0.4 - t * 0.22, t * 0.06) * 0.8 +
						noise(x * 1.2, z * 1.2 + t * 0.3, 3.3) * 0.16;
					for (const r of ripples) {
						const age = t - r.t;
						const d = Math.hypot(x - r.x, z - r.z);
						const front = d - age * 1.6;
						if (front > -1.2 && front < 0.3) {
							y += Math.sin(front * 9) * 0.12 * Math.exp(-age * 1.2) * Math.exp(-d * 0.4);
						}
					}
					const xr = x * cy + z * sy;
					const zr = -x * sy + z * cy;
					const yc = y - 1.35;
					const y2 = yc * cp - zr * sp;
					const z2 = yc * sp + zr * cp;
					if (z2 < 0.2) continue;
					const X = cx0 + (xr * scale) / z2;
					const Y = horizon - (y2 * scale) / z2;
					if (Y > height || Y < 0) continue;
					const near = Math.max(0, Math.min(1, 1 - (z2 - 0.3) / 7));
					const backlit = Math.pow(1 - near, 2.2) * (tone.back + y * 0.6) + Math.max(0, y) * 0.25;
					const light = Math.max(0, Math.min(1, near * tone.near + backlit));
					paths[Math.min(BUCKETS - 1, Math.floor(light * BUCKETS))].push(X, Y, 0.6 + near * 1.9);
				}
				ctx.globalCompositeOperation = 'lighter';
				for (let b = 0; b < BUCKETS; b += 1) {
					const path = paths[b];
					if (path.length === 0) continue;
					ctx.fillStyle = colours[b];
					ctx.beginPath();
					for (let q = 0; q < path.length; q += 3) {
						ctx.rect(path[q], path[q + 1], path[q + 2], path[q + 2]);
					}
					ctx.fill();
				}
				ctx.globalCompositeOperation = 'source-over';
			},
			destroy() {
				paths.length = 0;
			}
		};
	}

	/** Where a point on the screen meets the calm water, in sea units, or null
	 * when it is above the horizon or off the sea. */
	function water(sx: number, sy: number, camera: Camera): { x: number; z: number } | null {
		const k = (camera.horizon - sy) / camera.scale;
		const cp = Math.cos(PITCH);
		const sp = Math.sin(PITCH);
		const yc = -1.35;
		const denominator = sp + k * cp;
		if (denominator <= 0) return null;
		const zr = (yc * cp - k * yc * sp) / denominator;
		const z2 = yc * sp + zr * cp;
		if (z2 < 0.2) return null;
		const xr = ((sx - camera.width / 2) * z2) / camera.scale;
		const cy = Math.cos(camera.yaw);
		const sy2 = Math.sin(camera.yaw);
		const x = xr * cy - zr * sy2;
		const z = xr * sy2 + zr * cy;
		if (z < 0.3 || z > 7.3 || Math.abs(x) > 4.5) return null;
		return { x, z };
	}

	let horizon = $state(0);
	let dim = $state(0);
	let scale = $state(560);
	let updateMeasureTarget: ((target?: HTMLElement) => void) | null = null;

	$effect(() => {
		const target = horizonAt;
		untrack(() => updateMeasureTarget?.(target));
	});

	function sea(stage: HTMLElement) {
		return untrack(() => {
			const tone = TONES[variant];
			const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
			// The pointer listens on the whole footer, or on the field's container,
			// so the text above the sea stays pointer-transparent without losing it.
			const target = stage.closest('footer') ?? stage.parentElement ?? stage;
			const camera: Camera = { width: 1, height: 1, ratio: 1, horizon: 0, scale: 560, yaw: 0 };
			const ripples: Ripple[] = [];
			let aim = 0;
			let frame = 0;
			let visible = true;
			let lost = false;
			let last = 0;
			let t = 0;
			let active: Engine | null = null;
			let observedHorizon: HTMLElement | null = null;

			const measure = () => {
				const rect = stage.getBoundingClientRect();
				camera.width = Math.max(1, rect.width);
				camera.height = Math.max(1, rect.height);
				camera.ratio = Math.min(window.devicePixelRatio || 1, 2);
				// The boards were drawn at 1440 by 900 with a scale of 560.
				camera.scale =
					560 * Math.min(1.3, Math.max(0.45, camera.width / 1440, camera.height / 900));
				if (variant === 'footer' && horizonAt) {
					const mark = horizonAt.getBoundingClientRect();
					camera.horizon = mark.top - rect.top - mark.height * 0.45;
					dim = mark.bottom - rect.top;
				} else {
					camera.horizon = camera.height * 0.42;
				}
				horizon = camera.horizon;
				scale = camera.scale;
			};

			const draw = () => active?.frame(t, camera, ripples);
			const resize = () => {
				measure();
				active?.resize(camera);
				if (active && !frame) draw();
			};
			const sizer = new ResizeObserver(resize);
			sizer.observe(stage);

			const setHorizonTarget = (next?: HTMLElement) => {
				const element = next ?? null;
				if (observedHorizon === element) return;
				if (observedHorizon) sizer.unobserve(observedHorizon);
				observedHorizon = element;
				if (observedHorizon) sizer.observe(observedHorizon);
				resize();
			};
			setHorizonTarget(horizonAt);
			updateMeasureTarget = setHorizonTarget;
			resize();

			// A canvas that has handed out a WebGL context cannot give a 2D one, so
			// the fallback draws on a fresh canvas.
			const mount = () => {
				const node = document.createElement('canvas');
				node.setAttribute('aria-hidden', 'true');
				stage.append(node);
				return node;
			};
			let canvas = mount();
			let engine: Engine | null = gpuEngine(canvas, tone);
			const gpu = engine !== null;
			if (engine) {
				stage.dataset.engine = 'webgl2';
			} else {
				canvas.remove();
				canvas = mount();
				engine = canvasEngine(canvas, tone);
				if (engine) stage.dataset.engine = '2d';
			}
			if (engine) {
				active = engine;
			} else {
				canvas.remove();
				delete stage.dataset.engine;
			}

			const tick = (now: number) => {
				frame = requestAnimationFrame(tick);
				const elapsed = now - last;
				// The Canvas 2D version holds 30 frames a second.
				if (!gpu && elapsed < 1000 / 30 - 2) return;
				last = now;
				const dt = Math.min(0.05, elapsed / 1000);
				t += dt;
				camera.yaw += (aim - camera.yaw) * Math.min(1, dt * 4);
				while (ripples.length > 0 && t - ripples[0].t > RIPPLE_LIFE) ripples.shift();
				draw();
			};

			// Run only while the sea is on screen and the tab is showing, and never
			// under reduced motion; otherwise hold the last frame.
			const sync = () => {
				const running = Boolean(active) && !reduced && !lost && visible && !document.hidden;
				if (running && !frame) {
					last = performance.now();
					frame = requestAnimationFrame(tick);
				} else if (!running && frame) {
					cancelAnimationFrame(frame);
					frame = 0;
				}
			};

			const onMove = (event: PointerEvent) => {
				const rect = stage.getBoundingClientRect();
				const x = event.clientX - rect.left;
				const y = event.clientY - rect.top;
				aim = (Math.max(0, Math.min(1, x / rect.width)) - 0.5) * 0.12;
				const spot = water(x, y, camera);
				const previous = ripples[ripples.length - 1];
				if (spot && (!previous || t - previous.t > 0.25)) ripples.push({ ...spot, t });
			};
			const onLeave = () => {
				aim = 0;
			};

			if (active && !reduced) {
				target.addEventListener('pointermove', onMove as EventListener, { passive: true });
				target.addEventListener('pointerleave', onLeave);
			}
			// A lost context stops the loop; preventing the default asks the browser
			// to restore it, and a restored context needs its GL objects built again.
			const onLost = (event: Event) => {
				event.preventDefault();
				lost = true;
				sync();
			};
			const onRestored = () => {
				const rebuilt = gpuEngine(canvas, tone);
				if (!rebuilt) return;
				active = rebuilt;
				active.resize(camera);
				lost = false;
				draw();
				sync();
			};
			if (gpu) {
				canvas.addEventListener('webglcontextlost', onLost);
				canvas.addEventListener('webglcontextrestored', onRestored);
			}
			const watcher = active
				? new IntersectionObserver((entries) => {
						visible = entries[0]?.isIntersecting ?? true;
						sync();
					})
				: null;
			watcher?.observe(stage);
			document.addEventListener('visibilitychange', sync);
			active?.resize(camera);
			draw();
			sync();
			if (active) canvas.dataset.live = 'true';

			return () => {
				sizer.disconnect();
				watcher?.disconnect();
				document.removeEventListener('visibilitychange', sync);
				target.removeEventListener('pointermove', onMove as EventListener);
				target.removeEventListener('pointerleave', onLeave);
				if (gpu) {
					canvas.removeEventListener('webglcontextlost', onLost);
					canvas.removeEventListener('webglcontextrestored', onRestored);
				}
				if (updateMeasureTarget === setHorizonTarget) updateMeasureTarget = null;
				if (frame) cancelAnimationFrame(frame);
				frame = 0;
				active?.destroy();
				canvas.remove();
				delete stage.dataset.engine;
			};
		});
	}
</script>

<div
	class="particle-sea"
	data-variant={variant}
	aria-hidden="true"
	style:--sea-horizon="{horizon}px"
	style:--sea-scale="{scale}px"
	style:--sea-dim={dim ? `${dim}px` : undefined}
	{@attach sea}
>
	<div class="glow"></div>
	<div class="fade"></div>
</div>

<style>
	/* The glow is the sunrise behind the horizon: a blue radial gradient whose
	   dome is shaped by a second gradient used as a mask, fitted to the
	   luminance of the sunrise photograph the boards masked it with. It keeps
	   the boards' proportions (1.36 scales tall, its foot 0.47 scales under the
	   horizon) and may reach above the sea, so the container clips it. Below
	   the horizon the page colour fades back in over the near water. */
	.particle-sea {
		position: absolute;
		inset: 0;
		pointer-events: none;
	}

	.glow {
		position: absolute;
		inset-inline: 0;
		inset-block-start: calc(var(--sea-horizon) - var(--sea-scale) * 0.89);
		block-size: calc(var(--sea-scale) * 1.36);
		background: radial-gradient(
			ellipse 80% 66% at 50% 112%,
			#ffffff 0%,
			#dfe8ff 12%,
			#9db9ff 26%,
			#5f88ff 40%,
			#3a64f0 52%,
			#2440b8 66%,
			#16246e 82%,
			#0b1238 100%
		);
		mask-image: radial-gradient(
			ellipse 68% 68% at 50% 100%,
			rgb(0 0 0 / 86%) 0%,
			rgb(0 0 0 / 82%) 8%,
			rgb(0 0 0 / 77%) 17%,
			rgb(0 0 0 / 64%) 33%,
			rgb(0 0 0 / 47%) 50%,
			rgb(0 0 0 / 25%) 67%,
			rgb(0 0 0 / 8%) 83%,
			rgb(0 0 0 / 0%) 100%
		);
	}

	.fade {
		position: absolute;
		inset-inline: 0;
		inset-block: calc(var(--sea-horizon) + var(--sea-scale) * 0.36) 0;
		background: linear-gradient(
			180deg,
			rgb(7 9 15 / 0%) 0%,
			rgb(7 9 15 / 88%) 2.5rem,
			#07090f 9rem
		);
	}

	.particle-sea :global(canvas) {
		position: absolute;
		inset: 0;
		display: block;
		inline-size: 100%;
		block-size: 100%;
	}

	/* The footer's copyright line sits on the near water under the wordmark,
	   so the particles dim below the wordmark. */
	.particle-sea[data-variant='footer'] :global(canvas) {
		mask-image: linear-gradient(
			180deg,
			#000 var(--sea-dim, calc(100% - 10rem)),
			rgb(0 0 0 / 22%) calc(var(--sea-dim, calc(100% - 10rem)) + 3rem)
		);
	}
</style>
