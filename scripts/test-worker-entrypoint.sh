#!/usr/bin/env bash
# Proves deploy/docker/worker-entrypoint.sh: the root path is unchanged, /models
# is chowned only when a file is not owned by WORKER_UID (real find on owned and
# foreign-uid fixtures), the GPU device groups are collected for the ROCm and
# CUDA layouts, the group list is never empty, the shared host cache is never
# chowned and is refused when it overlaps a chowned path, and the home variables
# are set. The script runs as a copy whose container paths (/models, /dev and
# /tmp/worker-home) are rewritten to fixtures; chown, stat and setpriv are
# stubs. One case runs the real setpriv; it needs root (setgroups needs
# CAP_SETGID), so run this test as root or in a container to execute it.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
entry="$repo/deploy/docker/worker-entrypoint.sh"
# A failed mktemp must never leave tmp naming the worktree, or the trap would delete it.
raw_tmp="$(mktemp -d)" || { echo 'error: mktemp failed' >&2; exit 1; }
tmp="$(cd "$raw_tmp" && pwd -P)" || { echo 'error: cannot enter the temporary directory' >&2; exit 1; }
[[ -n "$tmp" && "$tmp" != / && -d "$tmp" ]] || { echo "error: bad temporary directory: $tmp" >&2; exit 1; }
trap 'rm -rf "$tmp"' EXIT
REAL_STAT="$(command -v stat)"
me="$(id -u)"
failures=0
skips=0

pass_case() { printf 'ok %s\n' "$1"; }
fail_case() {
	printf 'FAIL %s\n' "$1"
	if [[ -n "${2:-}" ]]; then printf '  %s\n' "$2"; fi
	failures=$((failures + 1))
}

mkdir -p "$tmp/stubs"

# chown only logs its arguments: the entrypoint must never name the host cache.
cat >"$tmp/stubs/chown" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${CHOWN_LOG:?}"
STUB

# stat reads the mode and group of a fake device node from STAT_GID_MAP
# ("path mode gid" lines).
cat >"$tmp/stubs/stat" <<'STUB'
#!/usr/bin/env bash
if [[ "$1 $2 $3" == "-L -c %a %g" && -n "${STAT_GID_MAP:-}" ]]; then
	while read -r path mode gid; do
		if [[ "$path" == "$4" ]]; then printf '%s %s\n' "$mode" "$gid"; exit 0; fi
	done <"$STAT_GID_MAP"
	echo "fake stat: no mode for $4" >&2
	exit 1
fi
exec "${REAL_STAT:?}" "$@"
STUB

# setpriv logs its arguments, then runs the command after -- as the same user.
cat >"$tmp/stubs/setpriv" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${SETPRIV_LOG:?}"
while (($#)); do
	if [[ "$1" == -- ]]; then shift; exec "$@"; fi
	shift
done
exit 99
STUB
chmod +x "$tmp"/stubs/*

# A fresh case: a models volume, a host cache, a home and fake device nodes.
new_case() {
	case_dir="$tmp/$1"
	mkdir -p "$case_dir/bin" "$case_dir/models" "$case_dir/hf/hub/models--x" \
		"$case_dir/dev/dri" "$case_dir/empty-dev/dri"
	: >"$case_dir/chown.log"
	: >"$case_dir/setpriv.log"
	: >"$case_dir/stat.map"
	local name
	for name in chown stat setpriv; do ln -s "$tmp/stubs/$name" "$case_dir/bin/$name"; done
	STAT_GID_MAP="$case_dir/stat.map"
	make_entry "$case_dir" "$case_dir/models"
}

# Writes the entrypoint copy for CASE_DIR, with /models set to MODELS_DIR and
# the device and home paths moved into the case. Each rewrite must land once.
make_entry() {  # CASE_DIR MODELS_DIR
	local copy="$1/entrypoint.sh"
	sed -e "s#^models_dir=/models\$#models_dir=$2#" \
		-e "s#^dev_dir=/dev\$#dev_dir=$1/dev#" \
		-e "s#^home_dir=/tmp/worker-home\$#home_dir=$1/home#" "$entry" >"$copy"
	if ! { grep -Fxq "models_dir=$2" "$copy" && grep -Fxq "dev_dir=$1/dev" "$copy" && grep -Fxq "home_dir=$1/home" "$copy"; }; then
		echo "error: could not rewrite the container paths in $entry" >&2
		exit 1
	fi
}

# Runs the entrypoint copy with a clean environment. Knobs are KEY=VALUE words.
run_entry() {  # CASE_DIR KNOB... -- COMMAND...
	local case_dir="$1" knobs=()
	shift
	while (($#)) && [[ "$1" != -- ]]; do knobs+=("$1"); shift; done
	shift
	env -i PATH="$case_dir/bin:/usr/bin:/bin" CHOWN_LOG="$case_dir/chown.log" \
		SETPRIV_LOG="$case_dir/setpriv.log" REAL_STAT="$REAL_STAT" STAT_GID_MAP="$STAT_GID_MAP" \
		HOME=/root-fixture HF_HUB_CACHE="$case_dir/hf/hub" \
		"${knobs[@]}" sh "$case_dir/entrypoint.sh" "$@" 2>&1
}

# The ROCm layout: /dev/kfd and /dev/dri/* in the video and render groups
# (mode 0660), plus the root-owned by-path directory, which adds no group.
rocm_layout() {  # CASE_DIR
	: >"$1/dev/kfd"
	: >"$1/dev/dri/card0"
	: >"$1/dev/dri/renderD128"
	mkdir -p "$1/dev/dri/by-path"
	printf '%s\n' "$1/dev/kfd 660 44" "$1/dev/dri/card0 660 44" "$1/dev/dri/renderD128 660 105" >"$1/stat.map"
}

# The CUDA layout with a restricted mode (NVreg_DeviceFileMode=0660): each
# nvidia node in its own group.
cuda_layout() {  # CASE_DIR
	: >"$1/dev/nvidia0"
	: >"$1/dev/nvidiactl"
	: >"$1/dev/nvidia-uvm"
	printf '%s\n' "$1/dev/nvidia0 660 110" "$1/dev/nvidiactl 660 111" "$1/dev/nvidia-uvm 660 112" >"$1/stat.map"
}

# The default CUDA layout: the NVIDIA Container Toolkit nodes are 0666 root:root.
cuda_default_layout() {  # CASE_DIR
	: >"$1/dev/nvidia0"
	: >"$1/dev/nvidiactl"
	: >"$1/dev/nvidia-uvm"
	printf '%s\n' "$1/dev/nvidia0 666 0" "$1/dev/nvidiactl 666 0" "$1/dev/nvidia-uvm 666 0" >"$1/stat.map"
}

# A models directory with one file this user does not own. As root the fixture
# gives a file to uid 1; otherwise an existing root-owned directory is used.
# Prints the directory, or nothing when no foreign file is available.
foreign_models() {  # CASE_DIR
	if [[ "$me" -eq 0 ]]; then
		mkdir -p "$1/foreign-models"
		: >"$1/foreign-models/foreign.json"
		chown 1 "$1/foreign-models/foreign.json" && printf '%s\n' "$1/foreign-models"
	elif [[ -n "$(find /etc -maxdepth 1 ! -uid "$me" -print -quit 2>/dev/null)" ]]; then
		printf '%s\n' /etc
	fi
}

# The owned fixture: one file that WORKER_UID owns in /models.
owned_models() {  # CASE_DIR
	: >"$1/models/owned.json"
}

# 1. Root path: WORKER_UID unset execs the command as it is, with no chown and no setpriv.
new_case root-path-unchanged
out="$(run_entry "$case_dir" -- sh -c 'echo ran as "$(id -u 2>/dev/null || echo ?)" home="$HOME"')" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$out" == 'ran as '*'home=/root-fixture' && ! -s "$case_dir/chown.log" && ! -s "$case_dir/setpriv.log" ]]; then
	pass_case root-path-unchanged
else
	fail_case root-path-unchanged "rc=$rc $out"
fi

# 2. Chown only when needed: a foreign file under /models chowns /models, nothing else.
new_case chown-when-foreign
rocm_layout "$case_dir"
if foreign="$(foreign_models "$case_dir")" && [[ -n "$foreign" ]]; then
	make_entry "$case_dir" "$foreign"
	out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 && "$(grep -c "^-R $me:2000 $foreign\$" "$case_dir/chown.log")" -eq 1 ]]; then
		pass_case chown-when-foreign
	else
		fail_case chown-when-foreign "rc=$rc $out $(cat "$case_dir/chown.log")"
	fi
else
	printf 'skip chown-when-foreign: no file owned by another uid is available (run as root to create one)\n'
	skips=$((skips + 1))
fi

# 3. Chown only when needed: every file already owned by WORKER_UID means no chown of /models.
new_case no-chown-when-owned
rocm_layout "$case_dir"
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(grep -c "$case_dir/models" "$case_dir/chown.log")" -eq 0 ]]; then
	pass_case no-chown-when-owned
else
	fail_case no-chown-when-owned "rc=$rc $out $(cat "$case_dir/chown.log")"
fi

# 4. The host cache (HF_HUB_CACHE, as Compose sets it) is never chowned, and the home directory is.
new_case host-cache-never-chowned
rocm_layout "$case_dir"
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(grep -c "$case_dir/hf" "$case_dir/chown.log")" -eq 0 && "$(grep -c "$case_dir/home" "$case_dir/chown.log")" -eq 1 ]]; then
	pass_case host-cache-never-chowned
else
	fail_case host-cache-never-chowned "rc=$rc $out $(cat "$case_dir/chown.log")"
fi

# 5. ROCm groups: WORKER_GID plus the gids of kfd and every dri node, without repeats.
new_case rocm-groups
rocm_layout "$case_dir"
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(cat "$case_dir/setpriv.log")" == "--reuid $me --regid 2000 --groups 2000,44,105 -- true" ]]; then
	pass_case rocm-groups
else
	fail_case rocm-groups "rc=$rc $out $(cat "$case_dir/setpriv.log")"
fi

# 6. CUDA groups: each nvidia node contributes its own group.
new_case cuda-groups
cuda_layout "$case_dir"
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
# The device glob sorts by the shell's collation, so the nodes are compared as a set.
groups="$(sed -n 's/.*--groups \([0-9,]*\) .*/\1/p' "$case_dir/setpriv.log")"
if [[ "$rc" -eq 0 && "$groups" == 2000,* && "$(printf '%s\n' "${groups#2000,}" | tr , '\n' | sort | tr '\n' ' ')" == '110 111 112 ' \
	&& "$(sed 's/--groups [0-9,]*/--groups X/' "$case_dir/setpriv.log")" == "--reuid $me --regid 2000 --groups X -- true" ]]; then
	pass_case cuda-groups
else
	fail_case cuda-groups "rc=$rc $out $(cat "$case_dir/setpriv.log")"
fi

# 6b. Default CUDA nodes (0666 root:root) add no group: never the root group.
new_case cuda-default-no-root-group
cuda_default_layout "$case_dir"
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(cat "$case_dir/setpriv.log")" == "--reuid $me --regid 2000 --groups 2000 -- true" ]]; then
	pass_case cuda-default-no-root-group
else
	fail_case cuda-default-no-root-group "rc=$rc $out $(cat "$case_dir/setpriv.log")"
fi

# 7. The group list is never empty: no device nodes leaves WORKER_GID alone.
new_case groups-never-empty
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- true)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(cat "$case_dir/setpriv.log")" == "--reuid $me --regid 2000 --groups 2000 -- true" ]]; then
	pass_case groups-never-empty
else
	fail_case groups-never-empty "rc=$rc $out $(cat "$case_dir/setpriv.log")"
fi

# 8. The home variables point at the private directory the worker owns.
new_case home-variables
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 -- sh -c 'printf "%s %s %s %s\n" "$HOME" "$USER" "$HF_HOME" "$TORCHINDUCTOR_CACHE_DIR"')" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$out" == "$case_dir/home worker $case_dir/home/hf $case_dir/home/inductor" ]]; then
	pass_case home-variables
else
	fail_case home-variables "rc=$rc $out"
fi

# 9. A WORKER_UID that is not a number, or a missing WORKER_GID, refuses to start.
new_case refuses-bad-ids
out="$(run_entry "$case_dir" WORKER_UID=abc WORKER_GID=2000 -- true)" && rc=0 || rc=$?
out2="$(run_entry "$case_dir" WORKER_UID="$me" -- true)" && rc2=0 || rc2=$?
if [[ "$rc" -ne 0 && "$out" == *'WORKER_UID must be a number'* && "$rc2" -ne 0 && "$out2" == *'WORKER_GID must be a number'* && ! -s "$case_dir/setpriv.log" ]]; then
	pass_case refuses-bad-ids
else
	fail_case refuses-bad-ids "rc=$rc/$rc2 $out $out2"
fi

# 10. A host cache inside, above or equal to /models or the home directory is refused before any chown.
new_case refuses-overlapping-cache
owned_models "$case_dir"
ln -s "$case_dir/models" "$case_dir/link-to-models"
overlap_ok=true
for cache in "$case_dir/models/hub" "$case_dir/home/hf" "$case_dir/home" "$case_dir" "$case_dir/link-to-models/hub"; do
	out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 HF_HUB_CACHE="$cache" -- true)" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 || "$out" != *'inside or above'* || -s "$case_dir/chown.log" || -s "$case_dir/setpriv.log" ]]; then
		overlap_ok=false
		fail_case refuses-overlapping-cache "cache=$cache rc=$rc $out"
	fi
done
if [[ "$overlap_ok" == true ]]; then pass_case refuses-overlapping-cache; fi

# 10b. STATE_DIR is refused when relative, or when it lies inside the host cache, before any chown.
new_case refuses-bad-state-dir
owned_models "$case_dir"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 STATE_DIR=relative/state -- true)" && rc=0 || rc=$?
out2="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 STATE_DIR="$case_dir/hf/hub/state" -- true)" && rc2=0 || rc2=$?
if [[ "$rc" -ne 0 && "$out" == *'STATE_DIR must be an absolute path'* && "$rc2" -ne 0 && "$out2" == *'inside or above'* \
	&& ! -s "$case_dir/chown.log" && ! -s "$case_dir/setpriv.log" ]]; then
	pass_case refuses-bad-state-dir
else
	fail_case refuses-bad-state-dir "rc=$rc/$rc2 $out $out2"
fi

# 10d. STATE_DIR that is, or is inside or above, /models or the home directory is refused before any chown:
# a recursive chown of it would change files that are not the worker's state.
new_case refuses-state-dir-over-owned-paths
owned_models "$case_dir"
bad=0
for state in "$case_dir/models" "$case_dir/models/state" "$case_dir" /; do
	out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 STATE_DIR="$state" -- true)" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 || "$out" != *'STATE_DIR'*'must lie outside'* ]]; then
		bad=1
		break
	fi
done
if [[ "$bad" -eq 0 && ! -s "$case_dir/chown.log" && ! -s "$case_dir/setpriv.log" ]]; then
	pass_case refuses-state-dir-over-owned-paths
else
	fail_case refuses-state-dir-over-owned-paths "state=$state rc=$rc $out"
fi

# 10c. STATE_DIR is chowned only when something in it is not owned by WORKER_UID, as /models is.
new_case state-dir-owned-not-chowned
owned_models "$case_dir"
mkdir -p "$case_dir/state"
: >"$case_dir/state/fleet-credential.json"
out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 STATE_DIR="$case_dir/state" -- true)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(grep -c "$case_dir/state" "$case_dir/chown.log")" -eq 0 ]]; then
	pass_case state-dir-owned-not-chowned
else
	fail_case state-dir-owned-not-chowned "rc=$rc $out $(cat "$case_dir/chown.log")"
fi

new_case state-dir-foreign-chowned
owned_models "$case_dir"
if foreign="$(foreign_models "$case_dir")" && [[ -n "$foreign" ]]; then
	out="$(run_entry "$case_dir" WORKER_UID="$me" WORKER_GID=2000 STATE_DIR="$foreign" -- true)" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 && "$(grep -c "^-R $me:2000 $foreign\$" "$case_dir/chown.log")" -eq 1 ]]; then
		pass_case state-dir-foreign-chowned
	else
		fail_case state-dir-foreign-chowned "rc=$rc $out $(cat "$case_dir/chown.log")"
	fi
else
	printf 'skip state-dir-foreign-chowned: no file owned by another uid is available (run as root to create one)\n'
	skips=$((skips + 1))
fi

# 11. Real setpriv with no device nodes: the process ends up with WORKER_UID, WORKER_GID and that group only.
if [[ "$me" -eq 0 ]]; then
	new_case real-setpriv-no-devices
	real_models="$case_dir/real-models"
	mkdir -p "$real_models"
	make_entry "$case_dir" "$real_models"
	out="$(env -i PATH=/usr/bin:/bin WORKER_UID=1234 WORKER_GID=1234 \
		sh "$case_dir/entrypoint.sh" sh -c 'id -u; id -g; id -G' 2>&1)" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 && "$(printf '%s\n' "$out" | tr '\n' ' ')" == '1234 1234 1234 ' ]]; then
		pass_case real-setpriv-no-devices
	else
		fail_case real-setpriv-no-devices "rc=$rc $out"
	fi
else
	printf 'skip real-setpriv-no-devices: needs root (setgroups needs CAP_SETGID); run this test as root or in a container\n'
	skips=$((skips + 1))
fi

if ((failures > 0)); then
	printf '%d worker entrypoint check(s) failed\n' "$failures"
	exit 1
fi
printf 'worker entrypoint checks ok (%d skipped)\n' "$skips"
