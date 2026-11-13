import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	awaitingMachine,
	builtInWorkers,
	fleetAddress,
	fleetPollInterval,
	formatTimeLeft,
	isLoopbackHost,
	isPrivateHttpHost,
	isSourceBuildImage,
	listedMembers,
	memberState,
	rowActions,
	type FleetMember,
	validLabel,
	workerCommand,
	wsAddress
} from './fleet-members.ts';

const NOW = Date.parse('2026-11-11T10:00:00Z');
const member = (overrides: Partial<FleetMember> = {}): FleetMember => ({
	id: 'm1',
	worker_id: 'm-1',
	label: 'Desk GPU',
	device: 'cuda',
	state: 'connected',
	paused: false,
	created_at: '2026-11-11T09:00:00Z',
	enrolled_at: '2026-11-11T09:05:00Z',
	last_seen_at: '2026-11-11T09:59:00Z',
	join_expires_at: null,
	...overrides
});

test('the worker address keeps the page host and switches to the websocket scheme', () => {
	assert.equal(wsAddress('http://192.168.1.20:8080'), 'ws://192.168.1.20:8080/api/v1/fleet');
	assert.equal(wsAddress('https://studio.example.com'), 'wss://studio.example.com/api/v1/fleet');
	assert.equal(
		wsAddress('https://studio.example.com:443'),
		'wss://studio.example.com/api/v1/fleet'
	);
});

test('loopback hosts are localhost, 127.0.0.1 and [::1] only', () => {
	assert.equal(isLoopbackHost('localhost'), true);
	assert.equal(isLoopbackHost('127.0.0.1'), true);
	assert.equal(isLoopbackHost('[::1]'), true);
	assert.equal(isLoopbackHost('192.168.1.20'), false);
	assert.equal(isLoopbackHost('localhost.example.com'), false);
});

test('a private IPv4 address is accepted over http in each range', () => {
	for (const host of [
		'10.0.0.1',
		'10.255.255.255',
		'172.16.0.1',
		'172.31.255.255',
		'192.168.0.1',
		'192.168.255.255',
		'100.64.0.1',
		'100.127.255.255'
	]) {
		assert.equal(isPrivateHttpHost(host), true, host);
	}
});

test('an IPv4 address just outside each private range, or a public one, is not private', () => {
	for (const host of [
		'9.255.255.255',
		'11.0.0.1',
		'172.15.255.255',
		'172.32.0.1',
		'192.167.255.255',
		'192.169.0.1',
		'100.63.255.255',
		'100.128.0.1',
		'8.8.8.8',
		'255.255.255.255'
	]) {
		assert.equal(isPrivateHttpHost(host), false, host);
	}
});

test('an IPv6 unique-local or link-local literal is private, other IPv6 is not', () => {
	for (const host of ['[fc00::1]', '[fd12:3456::1]', '[fe80::1]', '[febf::1]']) {
		assert.equal(isPrivateHttpHost(host), true, host);
	}
	for (const host of ['[fec0::1]', '[fe7f::1]', '[fe8::1]', '[2001:db8::1]', '[::]']) {
		assert.equal(isPrivateHttpHost(host), false, host);
	}
});

test('a single-label name or a private suffix is private, a public name is not', () => {
	for (const host of ['nas', 'printer.local', 'box.lan', 'app.home.arpa', 'db.internal']) {
		assert.equal(isPrivateHttpHost(host), true, host);
	}
	for (const host of [
		'studio.example.com',
		'example.com',
		'local.example.com',
		'internal.example.com',
		''
	]) {
		assert.equal(isPrivateHttpHost(host), false, host);
	}
});

test('a page on localhost asks for the address of this computer', () => {
	assert.deepEqual(fleetAddress('http://localhost:5173'), { kind: 'loopback' });
	assert.deepEqual(fleetAddress('https://localhost'), { kind: 'loopback' });
	assert.deepEqual(fleetAddress('http://127.0.0.1:8080'), { kind: 'loopback' });
	assert.deepEqual(fleetAddress('http://[::1]:8080'), { kind: 'loopback' });
});

test('https is accepted for any host, with the wss address', () => {
	assert.deepEqual(fleetAddress('https://studio.example.com'), {
		kind: 'secure',
		wsUrl: 'wss://studio.example.com/api/v1/fleet'
	});
	assert.deepEqual(fleetAddress('https://8.8.8.8'), {
		kind: 'secure',
		wsUrl: 'wss://8.8.8.8/api/v1/fleet'
	});
});

test('http on a private address is accepted as insecure', () => {
	assert.deepEqual(fleetAddress('http://192.168.1.20:8080'), {
		kind: 'insecure',
		wsUrl: 'ws://192.168.1.20:8080/api/v1/fleet'
	});
});

test('http on a public address is refused', () => {
	assert.deepEqual(fleetAddress('http://studio.example.com'), { kind: 'refused' });
	assert.deepEqual(fleetAddress('http://8.8.8.8'), { kind: 'refused' });
});

test('an address that is not an http or https origin is invalid', () => {
	assert.deepEqual(fleetAddress(''), { kind: 'invalid' });
	assert.deepEqual(fleetAddress('192.168.1.20:8080'), { kind: 'invalid' });
	assert.deepEqual(fleetAddress('ftp://192.168.1.20'), { kind: 'invalid' });
});

test('a label is 1 to 100 characters after trimming', () => {
	assert.equal(validLabel(''), false);
	assert.equal(validLabel('   '), false);
	assert.equal(validLabel('a'), true);
	assert.equal(validLabel('x'.repeat(100)), true);
	assert.equal(validLabel(` ${'x'.repeat(100)} `), true);
	assert.equal(validLabel('x'.repeat(101)), false);
});

test('the command for NVIDIA uses the GPU flag and the state, models and cache volumes', () => {
	assert.equal(
		workerCommand({
			device: 'cuda',
			wsUrl: 'wss://studio.example.com/api/v1/fleet',
			token: 'tok-123',
			image: 'ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-cuda',
			insecure: false
		}),
		[
			'docker run -d --name potocolom-worker --restart unless-stopped \\',
			'  --gpus all \\',
			'  -e DEVICE=cuda \\',
			'  -e API_URL=wss://studio.example.com/api/v1/fleet \\',
			'  -e JOIN_TOKEN=tok-123 \\',
			'  -e STATE_DIR=/state \\',
			'  -e MODELS_DIR=/models \\',
			'  -v potocolom-worker-state:/state \\',
			'  -v potocolom-models:/models \\',
			'  -v potocolom-hf-cache:/root/.cache/huggingface \\',
			'  ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-cuda'
		].join('\n')
	);
});

test('the command for AMD passes the ROCm devices and the video group', () => {
	assert.equal(
		workerCommand({
			device: 'rocm',
			wsUrl: 'ws://192.168.1.20:8080/api/v1/fleet',
			token: 'tok-456',
			image: 'ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-rocm',
			insecure: true
		}),
		[
			'docker run -d --name potocolom-worker --restart unless-stopped \\',
			'  --device /dev/kfd \\',
			'  --device /dev/dri \\',
			'  --group-add video \\',
			'  -e DEVICE=rocm \\',
			'  -e API_URL=ws://192.168.1.20:8080/api/v1/fleet \\',
			'  -e JOIN_TOKEN=tok-456 \\',
			'  -e STATE_DIR=/state \\',
			'  -e MODELS_DIR=/models \\',
			'  -e ALLOW_INSECURE_FLEET=true \\',
			'  -v potocolom-worker-state:/state \\',
			'  -v potocolom-models:/models \\',
			'  -v potocolom-hf-cache:/root/.cache/huggingface \\',
			'  ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-rocm'
		].join('\n')
	);
});

test('the insecure opt-in appears only when the address is insecure', () => {
	const secure = workerCommand({
		device: 'cuda',
		wsUrl: 'wss://studio.example.com/api/v1/fleet',
		token: 't',
		image: 'img',
		insecure: false
	});
	assert.equal(secure.includes('ALLOW_INSECURE_FLEET'), false);
	const insecure = workerCommand({
		device: 'cuda',
		wsUrl: 'ws://192.168.1.20:8080/api/v1/fleet',
		token: 't',
		image: 'img',
		insecure: true
	});
	assert.equal(insecure.includes('-e ALLOW_INSECURE_FLEET=true'), true);
});

test('time left counts down in minutes and seconds and never goes negative', () => {
	const expires = new Date(NOW + 900_000).toISOString();
	assert.equal(formatTimeLeft(expires, NOW), '15:00');
	assert.equal(formatTimeLeft(new Date(NOW + 61_000).toISOString(), NOW), '1:01');
	assert.equal(formatTimeLeft(new Date(NOW + 1000).toISOString(), NOW), '0:01');
	assert.equal(formatTimeLeft(new Date(NOW + 1).toISOString(), NOW), '0:01');
	assert.equal(formatTimeLeft(new Date(NOW - 5000).toISOString(), NOW), '0:00');
	assert.equal(formatTimeLeft('not a date', NOW), '0:00');
	// The clock that ticks the dialog can lag the server's by a second or more;
	// a join token never has more than its hour left.
	assert.equal(formatTimeLeft(new Date(NOW + 3_600_400).toISOString(), NOW), '60:00');
	assert.equal(formatTimeLeft(new Date(NOW + 3_700_000).toISOString(), NOW), '60:00');
});

test('a waiting member shows its time left, and reads as expired once the time has run out', () => {
	const waiting = member({
		state: 'waiting',
		join_expires_at: new Date(NOW + 840_000).toISOString()
	});
	assert.deepEqual(memberState(waiting, NOW), {
		key: 'app.admin.fleet_state_waiting',
		left: '14:00',
		seen: null
	});
	const lapsed = member({ state: 'waiting', join_expires_at: new Date(NOW - 1).toISOString() });
	assert.deepEqual(memberState(lapsed, NOW), {
		key: 'app.admin.fleet_state_expired',
		left: null,
		seen: null
	});
});

test('connected, expired and offline members carry their own state label', () => {
	assert.deepEqual(memberState(member({ state: 'connected' }), NOW), {
		key: 'app.admin.fleet_state_connected',
		left: null,
		seen: null
	});
	assert.deepEqual(memberState(member({ state: 'expired' }), NOW), {
		key: 'app.admin.fleet_state_expired',
		left: null,
		seen: null
	});
	assert.deepEqual(memberState(member({ state: 'offline' }), NOW), {
		key: 'app.admin.fleet_state_offline',
		left: null,
		seen: '2026-11-11T09:59:00Z'
	});
});

test('revoked members are not listed, the others are', () => {
	const rows = listedMembers([
		member({ id: 'a', state: 'revoked' }),
		member({ id: 'b', state: 'waiting' }),
		member({ id: 'c', state: 'offline' })
	]);
	assert.deepEqual(
		rows.map((row) => row.id),
		['b', 'c']
	);
});

test('built-in workers are the connected ids that do not start with m-', () => {
	assert.deepEqual(
		builtInWorkers([{ worker_id: 'builtin-1' }, { worker_id: 'm-7f3a' }, { worker_id: 'local' }]),
		['builtin-1', 'local']
	);
	assert.deepEqual(builtInWorkers([{ worker_id: 'm-7f3a' }]), []);
});

test('a row offers pause for an enrolled member and resume when it is paused', () => {
	assert.deepEqual(rowActions(member({ state: 'connected' })), { pause: 'pause', remove: true });
	assert.deepEqual(rowActions(member({ state: 'offline', paused: true })), {
		pause: 'resume',
		remove: true
	});
	assert.deepEqual(rowActions(member({ state: 'waiting' })), { pause: null, remove: true });
	assert.deepEqual(rowActions(member({ state: 'expired' })), { pause: null, remove: true });
	assert.deepEqual(rowActions(member({ state: 'revoked' })), { pause: null, remove: false });
});

test('the poll runs each second while the pending member waits, and each ten seconds otherwise', () => {
	assert.equal(fleetPollInterval(true, true), 1000);
	assert.equal(fleetPollInterval(true, false), 10000);
	assert.equal(fleetPollInterval(false, true), null);
	assert.equal(fleetPollInterval(false, false), null);
});

test('only a listed waiting member keeps the fast poll', () => {
	const members = [member({ id: 'w', state: 'waiting' }), member({ id: 'c' })];
	assert.equal(awaitingMachine(members, 'w'), true);
	assert.equal(awaitingMachine(members, 'c'), false);
	assert.equal(awaitingMachine(members, 'gone'), false);
	assert.equal(awaitingMachine(members, null), false);
});

test('an image from a source build is named, because another machine cannot pull it', () => {
	assert.equal(isSourceBuildImage('ghcr.io/portocolom-studio/potocolom-worker:dev-cuda'), true);
	assert.equal(isSourceBuildImage('ghcr.io/portocolom-studio/potocolom-worker:v0.1.4-rocm'), false);
	assert.equal(isSourceBuildImage('registry.example/dev-tools/worker:v1.0.0-cuda'), false);
});
