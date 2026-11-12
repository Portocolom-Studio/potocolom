#!/usr/bin/env bash
# Run a command with a private loopback-only network when the kernel allows it.
#
# Chrome aborts every in-flight request, loopback included, with
# ERR_NETWORK_CHANGED when the host adds or removes a network interface. CI
# jobs on the same machine create and remove Docker networks all the time
# (compose smoke tests, service containers), so browser checks failed at
# random (issues #726, #930). In its own network namespace the browser never
# sees those changes. The browser checks need no outside network.
set -euo pipefail
if unshare -rn true 2>/dev/null; then
	exec unshare -rn sh -c 'ip link set lo up && exec "$@"' sh "$@"
fi
echo "isolated-net: no unprivileged network namespace here; using the host network" >&2
exec "$@"
