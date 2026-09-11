type DrawingPoint = { x: number; y: number };

export type DrawingTool = 'draw' | 'erase' | 'line' | 'rectangle' | 'ellipse';

type GestureStyle = {
	tool: DrawingTool;
	color: string;
	size: number;
};

type StrokeOperation = {
	kind: 'stroke';
	id: string;
	mode: 'draw' | 'erase';
	color: string;
	size: number;
	points: DrawingPoint[];
};

type ClearOperation = {
	kind: 'clear';
	id: string;
};

type ShapeOperation = {
	kind: 'shape';
	id: string;
	shape: 'line' | 'rectangle' | 'ellipse';
	color: string;
	size: number;
	points: [DrawingPoint, DrawingPoint];
};

type Operation = StrokeOperation | ShapeOperation | ClearOperation;

export type DrawingFile = {
	version: 2;
	width: 512;
	height: 512;
	operations: Operation[];
	cursor: number;
};

type Checkpoint = {
	prefix: number;
	lastId: string;
	image: ImageBitmap;
};

const PAPER = '#ffffff';
const CHECKPOINT_INTERVAL = 16;
const MAX_CHECKPOINTS = 4;
export const DRAWING_FILE_MAX_BYTES = 8 * 1024 * 1024;
const MAX_OPERATIONS = 10_000;
const MAX_POINTS = 200_000;
const COORDINATE_MIN = -1_000_000;
const COORDINATE_MAX = 1_000_000;
const OPERATION_ID = /^operation-[0-9]+(?:-[0-9]+)*$/;
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

type ValidatedDrawing = {
	operations: Operation[];
	cursor: number;
};

export class DrawingDocument {
	private readonly canvas: HTMLCanvasElement;
	private readonly context: CanvasRenderingContext2D;
	private readonly operations: Operation[] = [];
	private readonly checkpoints: Checkpoint[] = [];
	private checkpointPending = false;
	private generation = 0;
	private cursor = 0;
	private active: {
		pointerId: number;
		operation: StrokeOperation | ShapeOperation;
		snapshot: HTMLCanvasElement | null;
	} | null = null;
	private blank = true;
	private destroyed = false;

	public constructor(canvas: HTMLCanvasElement) {
		const context = canvas.getContext('2d');
		if (!context) throw new Error('the drawing canvas has no 2D context');
		this.canvas = canvas;
		this.context = context;
		this.paintPaper();
	}

	public get isBlank(): boolean {
		if (this.active) this.updateBlank();
		return this.blank;
	}

	public get canUndo(): boolean {
		return this.cursor > 0 || this.active !== null;
	}

	public get canRedo(): boolean {
		return this.active === null && this.cursor < this.operations.length;
	}

	public serialize(): DrawingFile {
		const snapshot: DrawingFile = {
			version: 2,
			width: 512,
			height: 512,
			operations: this.operations.map((operation) =>
				operation.kind === 'clear'
					? { kind: 'clear', id: operation.id }
					: operation.kind === 'shape'
						? {
								kind: 'shape',
								id: operation.id,
								shape: operation.shape,
								color: operation.color,
								size: operation.size,
								points: [{ ...operation.points[0] }, { ...operation.points[1] }]
							}
						: {
								kind: 'stroke',
								id: operation.id,
								mode: operation.mode,
								color: operation.color,
								size: operation.size,
								points: operation.points.map((point) => ({ ...point }))
							}
			),
			cursor: this.cursor
		};
		validateDrawingFile(snapshot);
		return snapshot;
	}

	public restore(input: unknown): void {
		const validated = validateDrawingFile(input);
		if (this.destroyed) return;
		this.generation += 1;
		this.invalidateCheckpoints();
		this.releaseSnapshot();
		this.active = null;
		this.operations.length = 0;
		this.operations.push(...validated.operations);
		this.cursor = validated.cursor;
		this.render();
	}

	public beginStroke(pointerId: number, style: GestureStyle, point: DrawingPoint): boolean {
		if (this.destroyed || this.active !== null) return false;
		const tool = style.tool;
		if (tool !== 'draw' && tool !== 'erase') {
			const snapshot = document.createElement('canvas');
			snapshot.width = this.canvas.width;
			snapshot.height = this.canvas.height;
			const snapshotContext = snapshot.getContext('2d');
			if (!snapshotContext) return false;
			snapshotContext.drawImage(this.canvas, 0, 0);
			const operation: ShapeOperation = {
				kind: 'shape',
				id: this.newOperationId(),
				shape: tool,
				color: style.color,
				size: style.size,
				points: [{ ...point }, { ...point }]
			};
			this.active = { pointerId, operation, snapshot };
			this.paintShape(operation);
			return true;
		}
		const operation: StrokeOperation = {
			kind: 'stroke',
			id: this.newOperationId(),
			mode: tool,
			color: tool === 'erase' ? PAPER : style.color,
			size: style.size,
			points: [{ ...point }]
		};
		this.active = { pointerId, operation, snapshot: null };
		this.paintDot(operation, point);
		return true;
	}

	public extendStroke(pointerId: number, point: DrawingPoint): boolean {
		if (this.destroyed || !this.active || this.active.pointerId !== pointerId) return false;
		const { operation, snapshot } = this.active;
		if (operation.kind === 'shape') {
			if (!snapshot) return false;
			this.context.drawImage(snapshot, 0, 0);
			operation.points[1] = { ...point };
			this.paintShape(operation);
			return true;
		}
		this.paintSegment(operation, operation.points[operation.points.length - 1], point);
		operation.points.push({ ...point });
		return true;
	}

	public finishStroke(pointerId?: number): boolean {
		if (
			this.destroyed ||
			!this.active ||
			(pointerId !== undefined && this.active.pointerId !== pointerId)
		) {
			return false;
		}
		const operation = this.active.operation;
		this.releaseSnapshot();
		this.active = null;
		this.append(operation);
		// Read back at gesture boundaries; a full readback on every move stalls painting.
		this.updateBlank();
		this.scheduleCheckpoint();
		return true;
	}

	public undo(): boolean {
		if (this.destroyed) return false;
		this.finishStroke();
		if (this.cursor === 0) return false;
		this.cursor -= 1;
		this.render();
		return true;
	}

	public redo(): boolean {
		if (this.destroyed) return false;
		this.finishStroke();
		if (this.cursor >= this.operations.length) return false;
		this.cursor += 1;
		this.render();
		return true;
	}

	public clear(): boolean {
		if (this.destroyed) return false;
		this.finishStroke();
		if (this.blank) return false;
		this.append({ kind: 'clear', id: this.newOperationId() });
		this.paintPaper();
		this.blank = true;
		this.scheduleCheckpoint();
		return true;
	}

	private render(): void {
		if (this.destroyed) return;
		this.paintPaper();
		const checkpoint = this.bestCheckpoint();
		const start = checkpoint?.prefix ?? 0;
		if (checkpoint)
			this.context.drawImage(checkpoint.image, 0, 0, this.canvas.width, this.canvas.height);
		for (let index = start; index < this.cursor; index += 1) {
			this.paintOperation(this.operations[index]);
		}
		this.updateBlank();
	}

	public destroy(): void {
		if (this.destroyed) return;
		this.destroyed = true;
		this.generation += 1;
		this.releaseSnapshot();
		this.active = null;
		this.invalidateCheckpoints();
		this.operations.length = 0;
		this.cursor = 0;
	}

	private newOperationId(): string {
		// Unlike randomUUID, this also works on plain-HTTP self-hosted installs.
		return `operation-${crypto.getRandomValues(new Uint32Array(4)).join('-')}`;
	}

	private append(operation: Operation): void {
		this.operations.length = this.cursor;
		this.operations.push(operation);
		this.cursor = this.operations.length;
		this.pruneCheckpoints();
	}

	private paintPaper(): void {
		this.context.save();
		this.context.fillStyle = PAPER;
		this.context.fillRect(0, 0, this.canvas.width, this.canvas.height);
		this.context.restore();
	}

	private paintOperation(operation: Operation): void {
		if (operation.kind === 'clear') {
			this.paintPaper();
			return;
		}
		if (operation.kind === 'shape') {
			this.paintShape(operation);
			return;
		}
		this.paintDot(operation, operation.points[0]);
		for (let index = 1; index < operation.points.length; index += 1) {
			this.paintSegment(operation, operation.points[index - 1], operation.points[index]);
		}
	}

	private paintDot(operation: StrokeOperation, point: DrawingPoint): void {
		this.context.save();
		this.context.fillStyle = operation.color;
		this.context.beginPath();
		this.context.arc(point.x, point.y, operation.size / 2, 0, Math.PI * 2);
		this.context.fill();
		this.context.restore();
	}

	private paintSegment(operation: StrokeOperation, from: DrawingPoint, to: DrawingPoint): void {
		this.context.save();
		this.context.strokeStyle = operation.color;
		this.context.lineWidth = operation.size;
		this.context.lineCap = 'round';
		this.context.lineJoin = 'round';
		this.context.beginPath();
		this.context.moveTo(from.x, from.y);
		this.context.lineTo(to.x, to.y);
		this.context.stroke();
		this.context.restore();
	}

	private paintShape(operation: ShapeOperation): void {
		this.context.save();
		this.context.strokeStyle = operation.color;
		this.context.lineWidth = operation.size;
		this.context.lineCap = 'round';
		this.context.lineJoin = 'round';
		this.context.beginPath();
		const [start, end] = operation.points;
		if (operation.shape === 'line') {
			this.context.moveTo(start.x, start.y);
			this.context.lineTo(end.x, end.y);
		} else if (operation.shape === 'rectangle') {
			const left = Math.min(start.x, end.x);
			const top = Math.min(start.y, end.y);
			const width = Math.abs(end.x - start.x);
			const height = Math.abs(end.y - start.y);
			this.context.rect(left, top, width, height);
		} else {
			const centerX = (start.x + end.x) / 2;
			const centerY = (start.y + end.y) / 2;
			const radiusX = Math.abs(end.x - start.x) / 2;
			const radiusY = Math.abs(end.y - start.y) / 2;
			this.context.ellipse(centerX, centerY, radiusX, radiusY, 0, 0, Math.PI * 2);
		}
		this.context.stroke();
		this.context.restore();
	}

	private releaseSnapshot(): void {
		if (!this.active?.snapshot) return;
		this.active.snapshot.width = 0;
		this.active.snapshot.height = 0;
		this.active.snapshot = null;
	}

	private updateBlank(): void {
		const pixels = this.context.getImageData(0, 0, this.canvas.width, this.canvas.height).data;
		for (let index = 0; index < pixels.length; index += 4) {
			if (pixels[index] !== 255 || pixels[index + 1] !== 255 || pixels[index + 2] !== 255) {
				this.blank = false;
				return;
			}
		}
		this.blank = true;
	}

	private bestCheckpoint(): Checkpoint | undefined {
		let best: Checkpoint | undefined;
		for (const checkpoint of this.checkpoints) {
			if (
				checkpoint.prefix > this.cursor ||
				!this.prefixIsCurrent(checkpoint.prefix, checkpoint.lastId)
			)
				continue;
			if (!best || checkpoint.prefix > best.prefix) best = checkpoint;
		}
		return best;
	}

	private prefixIsCurrent(prefix: number, lastId: string): boolean {
		return this.operations[prefix - 1]?.id === lastId;
	}

	private pruneCheckpoints(): void {
		for (let index = this.checkpoints.length - 1; index >= 0; index -= 1) {
			const checkpoint = this.checkpoints[index];
			if (this.prefixIsCurrent(checkpoint.prefix, checkpoint.lastId)) continue;
			checkpoint.image.close();
			this.checkpoints.splice(index, 1);
		}
	}

	private invalidateCheckpoints(): void {
		for (const checkpoint of this.checkpoints) checkpoint.image.close();
		this.checkpoints.length = 0;
	}

	private scheduleCheckpoint(): void {
		if (this.destroyed || this.checkpointPending || this.cursor % CHECKPOINT_INTERVAL !== 0) return;
		this.checkpointPending = true;
		const generation = this.generation;
		const prefix = this.cursor;
		const lastId = this.operations[prefix - 1].id;
		// toBlob snapshots now, before another stroke or clear can change this prefix.
		this.canvas.toBlob(async (blob) => {
			try {
				if (
					!blob ||
					this.destroyed ||
					generation !== this.generation ||
					!this.prefixIsCurrent(prefix, lastId)
				)
					return;
				const image = await createImageBitmap(blob);
				if (
					this.destroyed ||
					generation !== this.generation ||
					!this.prefixIsCurrent(prefix, lastId)
				) {
					image.close();
					return;
				}
				const existing = this.checkpoints.findIndex((checkpoint) => checkpoint.prefix === prefix);
				if (existing >= 0) {
					this.checkpoints[existing].image.close();
					this.checkpoints.splice(existing, 1);
				}
				this.checkpoints.push({ prefix, lastId, image });
				this.checkpoints.sort((left, right) => left.prefix - right.prefix);
				while (this.checkpoints.length > MAX_CHECKPOINTS) {
					const evicted = this.checkpoints.shift();
					if (evicted) evicted.image.close();
				}
			} catch {
				// A failed cache fill falls back to journal replay, without changing artwork.
			} finally {
				this.checkpointPending = false;
			}
		}, 'image/png');
	}
}

function validateDrawingFile(input: unknown): ValidatedDrawing {
	let encodedBytes: number;
	try {
		const serialized = JSON.stringify(input);
		if (serialized === undefined) throw new Error('drawing file must be JSON');
		encodedBytes = new TextEncoder().encode(serialized).length;
	} catch {
		throw new Error('invalid drawing file');
	}
	if (encodedBytes > DRAWING_FILE_MAX_BYTES) throw new Error('drawing file is too large');
	if (!isRecord(input)) throw new Error('invalid drawing file');
	if ((input.version !== 1 && input.version !== 2) || input.width !== 512 || input.height !== 512)
		throw new Error('unsupported drawing file');
	if (!Array.isArray(input.operations) || input.operations.length > MAX_OPERATIONS)
		throw new Error('drawing file has too many operations');
	const cursor = input.cursor;
	if (
		typeof cursor !== 'number' ||
		!Number.isSafeInteger(cursor) ||
		cursor < 0 ||
		cursor > input.operations.length
	)
		throw new Error('invalid drawing cursor');

	const operations: Operation[] = [];
	const ids = new Set<string>();
	let totalPoints = 0;
	for (const rawOperation of input.operations) {
		if (!isRecord(rawOperation) || typeof rawOperation.kind !== 'string')
			throw new Error('invalid drawing operation');
		const id = validateOperationId(rawOperation.id, ids);
		if (rawOperation.kind === 'clear') {
			if (Object.keys(rawOperation).length !== 2) throw new Error('invalid clear operation');
			operations.push({ kind: 'clear', id });
			continue;
		}
		if (rawOperation.kind === 'shape') {
			if (input.version !== 2 || Object.keys(rawOperation).length !== 6)
				throw new Error('invalid shape operation');
			if (
				rawOperation.shape !== 'line' &&
				rawOperation.shape !== 'rectangle' &&
				rawOperation.shape !== 'ellipse'
			)
				throw new Error('invalid shape');
			if (typeof rawOperation.color !== 'string' || !HEX_COLOR.test(rawOperation.color))
				throw new Error('invalid shape color');
			const size = rawOperation.size;
			if (typeof size !== 'number' || !Number.isSafeInteger(size) || size < 1 || size > 32)
				throw new Error('invalid shape size');
			totalPoints += 2;
			if (totalPoints > MAX_POINTS) throw new Error('drawing file has too many points');
			const points = rawOperation.points;
			if (!Array.isArray(points) || points.length !== 2)
				throw new Error('shape must have two points');
			operations.push({
				kind: 'shape',
				id,
				shape: rawOperation.shape,
				color: rawOperation.color.toLowerCase(),
				size,
				points: [validatePoint(points[0]), validatePoint(points[1])]
			});
			continue;
		}
		if (rawOperation.kind !== 'stroke' || Object.keys(rawOperation).length !== 6)
			throw new Error('invalid stroke operation');
		if (rawOperation.mode !== 'draw' && rawOperation.mode !== 'erase')
			throw new Error('invalid stroke mode');
		if (typeof rawOperation.color !== 'string' || !HEX_COLOR.test(rawOperation.color))
			throw new Error('invalid stroke color');
		const color = rawOperation.color.toLowerCase();
		if (rawOperation.mode === 'erase' && color !== PAPER) throw new Error('invalid erase color');
		const size = rawOperation.size;
		if (typeof size !== 'number' || !Number.isSafeInteger(size) || size < 1 || size > 32)
			throw new Error('invalid stroke size');
		if (!Array.isArray(rawOperation.points) || rawOperation.points.length === 0)
			throw new Error('stroke must have points');
		totalPoints += rawOperation.points.length;
		if (totalPoints > MAX_POINTS) throw new Error('drawing file has too many points');
		const points = rawOperation.points.map(validatePoint);
		operations.push({
			kind: 'stroke',
			id,
			mode: rawOperation.mode,
			color,
			size,
			points
		});
	}
	return { operations, cursor };
}

function validateOperationId(input: unknown, ids: Set<string>): string {
	if (typeof input !== 'string' || input.length > 64 || !OPERATION_ID.test(input) || ids.has(input))
		throw new Error('invalid operation id');
	ids.add(input);
	return input;
}

function validatePoint(input: unknown): DrawingPoint {
	if (!isRecord(input) || Object.keys(input).length !== 2) throw new Error('invalid drawing point');
	if (
		typeof input.x !== 'number' ||
		typeof input.y !== 'number' ||
		!Number.isFinite(input.x) ||
		!Number.isFinite(input.y) ||
		input.x < COORDINATE_MIN ||
		input.x > COORDINATE_MAX ||
		input.y < COORDINATE_MIN ||
		input.y > COORDINATE_MAX
	)
		throw new Error('invalid drawing point');
	return { x: input.x, y: input.y };
}

function isRecord(input: unknown): input is Record<string, unknown> {
	return typeof input === 'object' && input !== null && !Array.isArray(input);
}
