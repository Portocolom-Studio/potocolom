import { pollInterval } from './model-management.ts';

export type FleetDevice = 'cuda' | 'rocm';
export type FleetState = 'waiting' | 'expired' | 'connected' | 'offline' | 'revoked';

export type FleetMember = {
	id: string;
	worker_id: string;
	label: string;
	device: FleetDevice;
	state: FleetState;
	paused: boolean;
	created_at: string;
	enrolled_at: string | null;
	last_seen_at: string | null;
	join_expires_at: string | null;
};

export type FleetStateKey =
	| 'app.admin.fleet_state_waiting'
	| 'app.admin.fleet_state_expired'
	| 'app.admin.fleet_state_connected'
	| 'app.admin.fleet_state_offline'
	| 'app.admin.fleet_state_revoked';

export type MemberState = {
	key: FleetStateKey;
	// Formatted time left, set only while a waiting member can still join.
	left: string | null;
	// The raw last_seen_at of an offline member, formatted by the component.
	seen: string | null;
};

export type FleetAddress =
	| { kind: 'secure'; wsUrl: string }
	| { kind: 'insecure'; wsUrl: string }
	| { kind: 'loopback' | 'refused' | 'invalid' };

// Matches the server's limit on the label (1 to 100 characters).
export function validLabel(label: string): boolean {
	const length = [...label.trim()].length;
	return length >= 1 && length <= 100;
}

export function wsAddress(origin: string): string {
	const url = new URL(origin);
	const scheme = url.protocol === 'https:' ? 'wss' : 'ws';
	return `${scheme}://${url.host}/api/v1/fleet`;
}

// A worker on another machine cannot reach these hosts, so the dialog must
// ask for the address of this computer instead.
export function isLoopbackHost(hostname: string): boolean {
	return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
}

export function isPrivateHttpHost(hostname: string): boolean {
	const host = hostname.toLowerCase();
	if (host.startsWith('[')) {
		const first = parseInt(host.slice(1).split(':')[0] || '0', 16);
		return (first & 0xfe00) === 0xfc00 || (first & 0xffc0) === 0xfe80;
	}
	const octets = host.split('.');
	if (octets.length === 4 && octets.every((octet) => /^\d{1,3}$/.test(octet))) {
		const [a, b] = octets.map(Number);
		if (a === undefined || b === undefined) return false;
		return (
			a === 10 ||
			(a === 172 && b >= 16 && b <= 31) ||
			(a === 192 && b === 168) ||
			(a === 100 && b >= 64 && b <= 127)
		);
	}
	if (host === '') return false;
	if (!host.includes('.')) return true;
	return ['.local', '.lan', '.home.arpa', '.internal'].some((suffix) => host.endsWith(suffix));
}

// Plain http is accepted only on a private network, because anyone on the path
// could read the join token. Every other host needs https.
export function fleetAddress(origin: string): FleetAddress {
	let url: URL;
	try {
		url = new URL(origin);
	} catch {
		return { kind: 'invalid' };
	}
	if (url.protocol !== 'http:' && url.protocol !== 'https:') return { kind: 'invalid' };
	if (isLoopbackHost(url.hostname)) return { kind: 'loopback' };
	const wsUrl = wsAddress(url.origin);
	if (url.protocol === 'https:') return { kind: 'secure', wsUrl };
	if (isPrivateHttpHost(url.hostname)) return { kind: 'insecure', wsUrl };
	return { kind: 'refused' };
}

export function workerCommand(input: {
	device: FleetDevice;
	wsUrl: string;
	token: string;
	image: string;
	insecure: boolean;
}): string {
	// The GPU arguments match docs/self-hosting.md (remote worker) and the
	// worker-rocm service in deploy/compose/compose.yml.
	const gpu =
		input.device === 'cuda'
			? ['--gpus all']
			: ['--device /dev/kfd', '--device /dev/dri', '--group-add video'];
	const lines = [
		'docker run -d --name potocolom-worker --restart unless-stopped',
		...gpu,
		`-e DEVICE=${input.device}`,
		`-e API_URL=${input.wsUrl}`,
		`-e JOIN_TOKEN=${input.token}`,
		'-e STATE_DIR=/state',
		'-e MODELS_DIR=/models',
		...(input.insecure ? ['-e ALLOW_INSECURE_FLEET=true'] : []),
		'-v potocolom-worker-state:/state',
		'-v potocolom-models:/models',
		'-v potocolom-hf-cache:/root/.cache/huggingface',
		input.image
	];
	return lines.map((line, index) => (index === 0 ? line : `  ${line}`)).join(' \\\n');
}

// A source build tags its images dev, and they exist only on the machine that
// built them, so the join command cannot pull one on another machine.
export function isSourceBuildImage(image: string): boolean {
	return /:dev-[a-z]+$/.test(image);
}

// A join token lives one hour on the server.
const JOIN_TOKEN_LIFE_MS = 3_600_000;

// Whole seconds, rounded up, so a token with a fraction of a second left
// still reads as time left rather than expired. Capped at the token's life:
// the clock that ticks the dialog can lag the server's, which read 60:01.
export function formatTimeLeft(expiresAt: string, now: number): string {
	const at = Date.parse(expiresAt);
	const ms = Number.isFinite(at) ? Math.min(JOIN_TOKEN_LIFE_MS, Math.max(0, at - now)) : 0;
	const seconds = Math.ceil(ms / 1000);
	return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

export function memberState(member: FleetMember, now: number): MemberState {
	switch (member.state) {
		case 'waiting': {
			const expires = member.join_expires_at === null ? NaN : Date.parse(member.join_expires_at);
			if (Number.isFinite(expires) && expires <= now)
				return { key: 'app.admin.fleet_state_expired', left: null, seen: null };
			return {
				key: 'app.admin.fleet_state_waiting',
				left: member.join_expires_at === null ? null : formatTimeLeft(member.join_expires_at, now),
				seen: null
			};
		}
		case 'expired':
			return { key: 'app.admin.fleet_state_expired', left: null, seen: null };
		case 'connected':
			return { key: 'app.admin.fleet_state_connected', left: null, seen: null };
		case 'offline':
			return { key: 'app.admin.fleet_state_offline', left: null, seen: member.last_seen_at };
		case 'revoked':
			return { key: 'app.admin.fleet_state_revoked', left: null, seen: null };
	}
}

// Revoked members are gone for the operator; they stay in the API only for audit.
export function listedMembers(members: FleetMember[]): FleetMember[] {
	return members.filter((member) => member.state !== 'revoked');
}

// The fleet workers start with "m-"; any other connected worker runs in the
// API process and cannot be paused or removed from here.
export function builtInWorkers(workers: { worker_id: string }[]): string[] {
	return workers.map((worker) => worker.worker_id).filter((id) => !id.startsWith('m-'));
}

export function rowActions(member: FleetMember): {
	pause: 'pause' | 'resume' | null;
	remove: boolean;
} {
	const enrolled = member.state === 'connected' || member.state === 'offline';
	return {
		pause: enrolled ? (member.paused ? 'resume' : 'pause') : null,
		remove: member.state !== 'revoked'
	};
}

// The same cadence as the model screen; the fleet only asks for the fast
// poll while the pending member is still waiting to join.
export function fleetPollInterval(mounted: boolean, awaiting: boolean): number | null {
	return pollInterval(mounted, awaiting);
}

export function awaitingMachine(members: FleetMember[], id: string | null): boolean {
	return id !== null && members.some((member) => member.id === id && member.state === 'waiting');
}
