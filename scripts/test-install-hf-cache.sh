#!/usr/bin/env bash
# Shared host cache for scripts/install.sh (POTOCOLOM_HF_CACHE): the detection
# line, the saved keys, the refusals before any change (a root-owned subtree,
# rootless Docker, a bad path, a link to a foreign tree, unsafe characters, a
# Hugging Face home directory, a path that overlaps /models or /tmp/worker-home),
# the resolved path that is saved, clearing the share, inherited variables that
# must not leak into .env or Compose, the default install, which must not
# change, and the Compose render of both workers with the real docker compose
# (skipped when it is not installed). Docker, curl, df, nvidia-smi, hostname,
# rocm-smi, sleep and awk are stubbed. install.sh, preflight.sh, ensure-env.sh
# and gpu-profile.sh run for real against a small compose stub. No network, no
# containers, no GPU. Run it as a normal user; refused-foreign-descendant also needs root
# (for example in a container), where the other cases refuse, since sharing refuses root.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# A failed mktemp must never leave tmp naming the worktree, or the trap would delete it.
raw_tmp="$(mktemp -d)" || { echo 'error: mktemp failed' >&2; exit 1; }
tmp="$(cd "$raw_tmp" && pwd -P)" || { echo 'error: cannot enter the temporary directory' >&2; exit 1; }
[[ -n "$tmp" && "$tmp" != / && -d "$tmp" ]] || { echo "error: bad temporary directory: $tmp" >&2; exit 1; }
trap 'rm -rf "$tmp"' EXIT
RELEASE_TEST_AWK="$(type -P awk)"
failures=0

pass_case() { printf 'ok %s\n' "$1"; }

fail_case() {
	printf 'FAIL %s\n' "$1"
	if [[ -n "${2:-}" ]]; then printf '  %s\n' "$2"; fi
	failures=$((failures + 1))
}

# ------------------------------------------------------------------ stubs

mkdir -p "$tmp/stubs"

cat >"$tmp/stubs/curl" <<'STUB'
#!/usr/bin/env bash
while (($#)); do
	if [[ "$1" == -o ]]; then
		out="$2"
		shift 2
	elif [[ "$1" == http://* || "$1" == https://* || "$1" == file://* ]]; then
		url="$1"
		shift
	else
		shift
	fi
done
if [[ -z "${out:-}" ]]; then
	if [[ "${url:-}" == */api/v1/ready ]]; then exit 0; fi
	printf '{"status":"ok"}\n'
	exit 0
fi
case "$out" in
	*SHA256SUMS) cp "$BUNDLE_DIR/SHA256SUMS" "$out" ;;
	*) cp "$BUNDLE_DIR/bundle.tar.gz" "$out" ;;
esac
STUB

cat >"$tmp/stubs/awk" <<'STUB'
#!/usr/bin/env bash
if [[ $# == 2 && "$2" == /proc/meminfo ]]; then
	printf '33554432\n'
else
	exec "${RELEASE_TEST_AWK:?}" "$@"
fi
STUB

cat >"$tmp/stubs/df" <<'STUB'
#!/usr/bin/env bash
if [[ "$*" == *-B1* ]]; then
	printf 'Avail\n1000000000000\n'
else
	printf 'Avail\n100G\n'
fi
STUB

cat >"$tmp/stubs/nvidia-smi" <<'STUB'
#!/usr/bin/env bash
exit 9
STUB

cat >"$tmp/stubs/hostname" <<'STUB'
#!/usr/bin/env bash
if [[ "$1" == -I ]]; then printf '192.168.1.10\n'; fi
STUB

cat >"$tmp/stubs/rocm-smi" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

cat >"$tmp/stubs/sleep" <<'STUB'
#!/usr/bin/env bash
exit 0
STUB

# The compose stub answers what the installer asks: the env file values, the
# image list, pull, up, and the setup link. Each call is logged with the shared
# cache variables the installer's shell exported, so a test can see a leak.
cat >"$tmp/stubs/docker" <<'STUB'
#!/usr/bin/env bash
state="${STATE_DIR:?}"
printf '%s | HF_CACHE_DIR=%s WORKER_UID=%s WORKER_GID=%s\n' "$*" \
	"${HF_CACHE_DIR-unset}" "${WORKER_UID-unset}" "${WORKER_GID-unset}" >>"${DOCKER_LOG:?}"

compose_cmd() {
	local env_file="" sub version
	while (($#)); do
		case "$1" in
			-p | -f | --profile) shift 2 ;;
			--env-file) env_file="$2"; shift 2 ;;
			*) break ;;
		esac
	done
	sub="${1:-}"
	shift || true
	case "$sub" in
		version) printf 'Docker Compose version v2.30.0\n' ;;
		config)
			if [[ "${1:-}" == --environment ]]; then
				if [[ -f "$env_file" ]]; then grep -v '^[[:space:]]*#' "$env_file" || true; fi
				return 0
			fi
			version="${POTOCOLOM_VERSION:-}"
			if [[ -z "$version" && -f "$env_file" ]]; then
				version="$(sed -n 's/^POTOCOLOM_VERSION=//p' "$env_file" | tail -n 1)"
			fi
			printf 'postgres:16\nghcr.io/portocolom-studio/potocolom-api:%s\n' "${version:-dev}"
			;;
		exec) printf 'http://192.168.1.10:8080/setup#installer-fixture-token\n' ;;
	esac
	return 0
}

case "${1:-}" in
	--version) printf 'Docker version 28.0.0, build test\n' ;;
	info)
		# SecurityOptions names rootless mode when the case asks for it.
		if [[ "$*" == *SecurityOptions* ]]; then
			if [[ "${ROOTLESS:-0}" == 1 ]]; then printf '[name=seccomp name=rootless]\n'; else printf '[name=seccomp]\n'; fi
		fi
		;;
	image) exit 1 ;;
	compose)
		shift
		compose_cmd "$@"
		exit $?
		;;
esac
exit 0
STUB

chmod +x "$tmp"/stubs/*

# ---------------------------------------------------------------- fixtures

new_case() {
	case_dir="$tmp/$1"
	mkdir -p "$case_dir/bin" "$case_dir/state" "$case_dir/source" "$case_dir/tmp" \
		"$case_dir/hub/models--stabilityai--sd" "$case_dir/hub/models--black-forest-labs--flux" \
		"$case_dir/hub-empty" "$case_dir/dri" "$case_dir/pci"
	: >"$case_dir/docker.log"
	: >"$case_dir/curl.log"
	: >"$case_dir/state/containers"
	local name
	for name in curl awk df nvidia-smi hostname rocm-smi sleep docker; do
		ln -s "$tmp/stubs/$name" "$case_dir/bin/$name"
	done
}

make_bundle() {  # CASE_DIR VERSION
	local case_dir="$1" version="$2" src="$1/bundle-src"
	if [[ ! -d "$src" ]]; then
		mkdir -p "$src/deploy/compose" "$src/scripts"
		cp "$repo/deploy/compose/compose.yml" "$repo/deploy/compose/.env.example" "$src/deploy/compose/"
		cp "$repo/scripts/preflight.sh" "$repo/scripts/ensure-env.sh" "$repo/scripts/install-lock.sh" \
			"$repo/scripts/gpu-profile.sh" "$repo/scripts/backup.sh" "$repo/scripts/restore.sh" "$src/scripts/"
		chmod +x "$src"/scripts/*.sh
		(cd "$src" && tar -czf "$case_dir/source/bundle.tar.gz" deploy scripts)
	fi
	(cd "$case_dir/source" && sha256sum bundle.tar.gz | sed "s#bundle.tar.gz#potocolom-$version-selfhost.tar.gz#" >SHA256SUMS)
}

# A live install left by an earlier release, saved with the profile api so no
# GPU is looked for. The optional third argument is extra .env text.
seed_live() {  # CASE_DIR VERSION [EXTRA_ENV_TEXT]
	local dir="$1/install"
	mkdir -p "$dir/deploy/compose" "$dir/scripts"
	cp "$repo/deploy/compose/compose.yml" "$dir/deploy/compose/"
	cp "$repo/scripts/backup.sh" "$repo/scripts/restore.sh" "$dir/scripts/"
	sed -e "s/^POTOCOLOM_VERSION=.*/POTOCOLOM_VERSION=$2/" \
		-e 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=keep-db/' \
		-e 's/^FLEET_SECRET=.*/FLEET_SECRET=keep-fleet/' \
		"$repo/deploy/compose/.env.example" >"$dir/deploy/compose/.env"
	printf 'COMPOSE_PROFILES=api\n' >>"$dir/deploy/compose/.env"
	if [[ -n "${3:-}" ]]; then printf '%s\n' "$3" >>"$dir/deploy/compose/.env"; fi
	chmod 600 "$dir/deploy/compose/.env"
}

tree_hash() {
	(cd "$1" && { find . -print | LC_ALL=C sort; find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum; }) | sha256sum | cut -d' ' -f1
}

# Runs install.sh for a case. Knobs are KEY=VALUE words after the case dir. The
# host cache default is an empty directory, so no real cache is ever seen.
run_install() {
	local case_dir="$1"
	shift
	env -u DISPLAY -u WAYLAND_DISPLAY -u POTOCOLOM_PROFILE -u COMPOSE_PROFILES -u POTOCOLOM_PUBLIC_URL \
		-u POTOCOLOM_HF_CACHE -u HF_CACHE_DIR -u WORKER_UID -u WORKER_GID -u HF_HOME \
		PATH="$case_dir/bin:$PATH" TMPDIR="$case_dir/tmp" STATE_DIR="$case_dir/state" \
		DOCKER_LOG="$case_dir/docker.log" CURL_LOG="$case_dir/curl.log" BUNDLE_DIR="$case_dir/source" \
		READY_COUNT_FILE="$case_dir/ready-count" RELEASE_TEST_AWK="$RELEASE_TEST_AWK" \
		POTOCOLOM_SKIP_GPU_TEST=1 HF_HUB_CACHE="$case_dir/hub-empty" \
		POTOCOLOM_KFD="$case_dir/no-kfd" POTOCOLOM_KFD_TOPOLOGY="$case_dir/kfd" \
		POTOCOLOM_DRI="$case_dir/dri" POTOCOLOM_PCI_DEVICES="$case_dir/pci" \
		POTOCOLOM_VERSION=v9.9.9 POTOCOLOM_DIR="$case_dir/install" \
		"$@" bash "$repo/scripts/install.sh" 2>&1
}

env_value() {  # CASE_DIR KEY: the value of KEY in the live .env, or nothing
	sed -n "s/^$2=//p" "$1/install/deploy/compose/.env" | tail -n 1
}

# ----------------------------------------------------------------- cases

# 0. The example names no shared-cache key, so ensure-env.sh never writes one empty.
if grep -Eq '^[[:space:]]*(HF_CACHE_DIR|WORKER_UID|WORKER_GID)[[:space:]]*=' "$repo/deploy/compose/.env.example"; then
	fail_case example-names-no-cache-key ".env.example names a shared-cache key"
else
	pass_case example-names-no-cache-key
fi

# 1. Detected and not set: the line names the path and the count, and nothing is saved.
new_case detected-not-set
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" HF_HUB_CACHE="$case_dir/hub")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *"Found 2 Hugging Face model repositories in $case_dir/hub."* \
	&& "$output" == *"POTOCOLOM_HF_CACHE=$case_dir/hub"* && -z "$(env_value "$case_dir" HF_CACHE_DIR)" ]]; then
	pass_case detected-not-set
else
	fail_case detected-not-set "rc=$rc $output"
fi

# 2. Default install: nothing detected, nothing saved, no shared-cache key in .env.
new_case default-install-unchanged
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" != *'Hugging Face model repositories'* \
	&& -z "$(env_value "$case_dir" HF_CACHE_DIR)" && -z "$(env_value "$case_dir" WORKER_UID)" \
	&& "$output" == *'model cache: the hf-cache Docker volume'* ]]; then
	pass_case default-install-unchanged
else
	fail_case default-install-unchanged "rc=$rc $output"
fi

# 3. Set: the path, and the owner's uid and gid, are saved, and the stack starts.
new_case shared-when-set
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/hub")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(env_value "$case_dir" HF_CACHE_DIR)" == "$case_dir/hub" \
	&& "$(env_value "$case_dir" WORKER_UID)" == "$(id -u)" && "$(env_value "$case_dir" WORKER_GID)" == "$(id -g)" \
	&& "$output" == *"model cache $case_dir/hub (host directory"* \
	&& "$(grep -c 'up -d --no-build' "$case_dir/docker.log")" -eq 1 ]]; then
	pass_case shared-when-set
else
	fail_case shared-when-set "rc=$rc $output"
fi

# 4. Refused for a root-owned subtree: /etc holds files this user does not own.
new_case refused-root-owned-subtree
seed_live "$case_dir" v8.8.8
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE=/etc)" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
if [[ "$rc" -ne 0 && "$before" == "$after" && "$(grep -c 'up -d --no-build' "$case_dir/docker.log")" -eq 0 \
	&& ( "$output" == *'sudo chown -R'* || "$output" == *'as root'* ) ]]; then
	pass_case refused-root-owned-subtree
else
	fail_case refused-root-owned-subtree "rc=$rc $output"
fi

# 4b. Refused for a hub this user owns that holds one file owned by someone else. Making
# that file needs root, so the case runs only as root; the installer then sees uid 4242.
if [[ "$(id -u)" -eq 0 ]]; then
	new_case refused-foreign-descendant
	seed_live "$case_dir" v8.8.8
	make_bundle "$case_dir" v9.9.9
	mkdir -p "$case_dir/owned-hub/models--org--repo/blobs"
	touch "$case_dir/owned-hub/models--org--repo/blobs/one" "$case_dir/owned-hub/models--org--repo/blobs/two"
	chown -R 4242:4242 "$case_dir/owned-hub"
	chown 0:0 "$case_dir/owned-hub/models--org--repo/blobs/two"
	printf '#!/bin/sh\ncase "$1" in -u|-g) echo 4242 ;; *) exec %s "$@" ;; esac\n' "$(type -P id)" >"$case_dir/bin/id"
	chmod +x "$case_dir/bin/id"
	before="$(tree_hash "$case_dir/install")"
	output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/owned-hub")" && rc=0 || rc=$?
	after="$(tree_hash "$case_dir/install")"
	if [[ "$rc" -ne 0 && "$before" == "$after" && "$output" == *'sudo chown -R'* ]]; then
		pass_case refused-foreign-descendant
	else
		fail_case refused-foreign-descendant "rc=$rc $output"
	fi
else
	printf 'skip refused-foreign-descendant: needs root to create a file owned by another user\n'
fi

# 5. Refused for rootless Docker, with the install directory unchanged.
new_case refused-rootless
seed_live "$case_dir" v8.8.8
make_bundle "$case_dir" v9.9.9
before="$(tree_hash "$case_dir/install")"
output="$(run_install "$case_dir" ROOTLESS=1 POTOCOLOM_HF_CACHE="$case_dir/hub")" && rc=0 || rc=$?
after="$(tree_hash "$case_dir/install")"
if [[ "$rc" -ne 0 && "$output" == *'rootless'* && "$before" == "$after" && -z "$(env_value "$case_dir" HF_CACHE_DIR)" ]]; then
	pass_case refused-rootless
else
	fail_case refused-rootless "rc=$rc $output"
fi

# 6. Refused before any change for a missing directory and for a relative path.
new_case refused-bad-path
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/missing")" && rc=0 || rc=$?
output2="$(run_install "$case_dir" POTOCOLOM_HF_CACHE=relative/hub)" && rc2=0 || rc2=$?
if [[ "$rc" -ne 0 && "$output" == *'is not a directory'* && "$rc2" -ne 0 && "$output2" == *'absolute path'* \
	&& ! -e "$case_dir/install" ]]; then
	pass_case refused-bad-path
else
	fail_case refused-bad-path "rc=$rc/$rc2 $output $output2"
fi

# 7. Cleared: an empty POTOCOLOM_HF_CACHE removes the three keys and keeps the rest of .env.
new_case cleared-by-empty-value
seed_live "$case_dir" v8.8.8 "$(printf 'HF_CACHE_DIR=%s\nWORKER_UID=1000\nWORKER_GID=1000' "$case_dir/hub")"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE=)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -z "$(env_value "$case_dir" HF_CACHE_DIR)" && -z "$(env_value "$case_dir" WORKER_UID)" \
	&& -z "$(env_value "$case_dir" WORKER_GID)" && "$(env_value "$case_dir" POSTGRES_PASSWORD)" == keep-db \
	&& "$output" == *'model cache: the hf-cache Docker volume'* ]]; then
	pass_case cleared-by-empty-value
else
	fail_case cleared-by-empty-value "rc=$rc $output"
fi

# 8. Upgrade keeps a saved share: the keys survive, no detection line, and preflight reports the cache.
new_case upgrade-keeps-share
seed_live "$case_dir" v8.8.8 "$(printf 'HF_CACHE_DIR=%s\nWORKER_UID=%s\nWORKER_GID=%s' "$case_dir/hub" "$(id -u)" "$(id -g)")"
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(env_value "$case_dir" HF_CACHE_DIR)" == "$case_dir/hub" \
	&& "$(env_value "$case_dir" WORKER_UID)" == "$(id -u)" && "$output" != *'Hugging Face model repositories'* \
	&& "$output" == *"model cache $case_dir/hub (host directory"* ]]; then
	pass_case upgrade-keeps-share
else
	fail_case upgrade-keeps-share "rc=$rc $output"
fi

# 9. An existing install without a share upgrades, and only names the detected cache.
new_case existing-install-upgraded
seed_live "$case_dir" v8.8.8
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" HF_HUB_CACHE="$case_dir/hub")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$output" == *'Upgrading v8.8.8 to v9.9.9'* && "$output" == *'Found 2 Hugging Face model repositories'* \
	&& -z "$(env_value "$case_dir" HF_CACHE_DIR)" ]]; then
	pass_case existing-install-upgraded
else
	fail_case existing-install-upgraded "rc=$rc $output"
fi

# 10. Inherited shared-cache variables do not reach .env or Compose.
new_case inherited-variables-unset
make_bundle "$case_dir" v9.9.9
output="$(run_install "$case_dir" HF_CACHE_DIR=/inherited WORKER_UID=5 WORKER_GID=5)" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && -z "$(env_value "$case_dir" HF_CACHE_DIR)" && -z "$(env_value "$case_dir" WORKER_UID)" \
	&& "$(grep -c 'HF_CACHE_DIR=unset WORKER_UID=unset WORKER_GID=unset' "$case_dir/docker.log")" -ge 1 \
	&& "$(grep -c 'HF_CACHE_DIR=/inherited' "$case_dir/docker.log")" -eq 0 ]]; then
	pass_case inherited-variables-unset
else
	fail_case inherited-variables-unset "rc=$rc $output"
fi


# 11. A link to the hub is resolved: the saved value is the real directory, and the detection line counts through the link.
new_case linked-hub-detected
make_bundle "$case_dir" v9.9.9
ln -s "$case_dir/hub" "$case_dir/linked-hub"
output="$(run_install "$case_dir" HF_HUB_CACHE="$case_dir/linked-hub")" && rc=0 || rc=$?
detected_ok=false
if [[ "$rc" -eq 0 && "$output" == *"Found 2 Hugging Face model repositories in $case_dir/hub."* ]]; then
	detected_ok=true
	pass_case linked-hub-detected
else
	fail_case linked-hub-detected "rc=$rc $output"
fi
new_case linked-hub-saves-resolved-path
make_bundle "$case_dir" v9.9.9
ln -s "$case_dir/hub" "$case_dir/linked-hub"
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/linked-hub")" && rc=0 || rc=$?
if [[ "$detected_ok" == true && "$rc" -eq 0 && "$(env_value "$case_dir" HF_CACHE_DIR)" == "$case_dir/hub" \
	&& "$output" == *"will share the host cache $case_dir/hub,"* ]]; then
	pass_case linked-hub-saves-resolved-path
else
	fail_case linked-hub-saves-resolved-path "rc=$rc detected=$detected_ok $output"
fi

# 12. A user-owned link to a tree with files of another owner is refused before any change.
new_case refused-linked-foreign-cache
make_bundle "$case_dir" v9.9.9
ln -s /etc "$case_dir/linked-foreign"
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/linked-foreign")" && rc=0 || rc=$?
if [[ "$rc" -ne 0 && ! -e "$case_dir/install" && "$(grep -c 'up -d --no-build' "$case_dir/docker.log")" -eq 0 \
	&& ( "$output" == *'does not own'* || "$output" == *'as root'* ) ]]; then
	pass_case refused-linked-foreign-cache
else
	fail_case refused-linked-foreign-cache "rc=$rc $output"
fi

# 13. Only letters, digits and . _ / @ + - are saved: a space, a dollar sign and a new line are refused; a path of allowed characters is saved.
new_case refused-unsafe-characters
make_bundle "$case_dir" v9.9.9
mkdir -p "$case_dir/sp ace/hub" "$case_dir/dol\$lar/hub" "$case_dir/new"$'\n'"line/hub"
unsafe_ok=true
for name in 'sp ace' 'dol$lar' "new"$'\n'"line"; do
	output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/$name/hub")" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 || "$output" != *'has a character that is not allowed'* || -e "$case_dir/install" ]]; then
		unsafe_ok=false
		fail_case refused-unsafe-characters "name=$name rc=$rc $output"
	fi
done
if [[ "$unsafe_ok" == true ]]; then pass_case refused-unsafe-characters; fi

new_case saved-allowed-characters
make_bundle "$case_dir" v9.9.9
mkdir -p "$case_dir/ok@1.2+x_y-z/hub"
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/ok@1.2+x_y-z/hub")" && rc=0 || rc=$?
if [[ "$rc" -eq 0 && "$(env_value "$case_dir" HF_CACHE_DIR)" == "$case_dir/ok@1.2+x_y-z/hub" ]]; then
	pass_case saved-allowed-characters
else
	fail_case saved-allowed-characters "rc=$rc $output"
fi

# 14. A Hugging Face home directory is refused, with its hub child named: one with a token file, and one called huggingface with a hub child.
new_case refused-hugging-face-home
make_bundle "$case_dir" v9.9.9
mkdir -p "$case_dir/hf-home/hub/models--x" "$case_dir/huggingface/hub"
: >"$case_dir/hf-home/token"
output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/hf-home")" && rc=0 || rc=$?
output2="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$case_dir/huggingface")" && rc2=0 || rc2=$?
if [[ "$rc" -ne 0 && "$output" == *'Hugging Face home directory'* && "$output" == *"$case_dir/hf-home/hub"* \
	&& "$rc2" -ne 0 && "$output2" == *'Hugging Face home directory'* && "$output2" == *"$case_dir/huggingface/hub"* \
	&& ! -e "$case_dir/install" ]]; then
	pass_case refused-hugging-face-home
else
	fail_case refused-hugging-face-home "rc=$rc/$rc2 $output $output2"
fi

# 15. A path that overlaps /models or /tmp/worker-home is refused before any change, whether or not it exists.
new_case refused-overlapping-paths
make_bundle "$case_dir" v9.9.9
overlap_ok=true
for path in /models /models/hub /tmp/worker-home /tmp/worker-home/hf /tmp /; do
	output="$(run_install "$case_dir" POTOCOLOM_HF_CACHE="$path")" && rc=0 || rc=$?
	if [[ "$rc" -eq 0 || "$output" != *'inside or above'* || -e "$case_dir/install" \
		|| "$(grep -c 'up -d --no-build' "$case_dir/docker.log")" -ne 0 ]]; then
		overlap_ok=false
		fail_case refused-overlapping-paths "path=$path rc=$rc $output"
	fi
done
if [[ "$overlap_ok" == true ]]; then pass_case refused-overlapping-paths; fi

# 16. The Compose render of both workers, default and shared, with the real docker compose: no daemon, no containers.
real_docker="$(type -P docker || true)"
if [[ -n "$real_docker" ]] && "$real_docker" compose version >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
	new_case compose-render
	printf 'POSTGRES_PASSWORD=render-fixture\nFLEET_SECRET=render-fixture\n' >"$case_dir/render-default.env"
	printf 'POSTGRES_PASSWORD=render-fixture\nFLEET_SECRET=render-fixture\nHF_CACHE_DIR=%s\nWORKER_UID=1000\nWORKER_GID=1000\n' \
		"$case_dir/hub" >"$case_dir/render-shared.env"
	# Prints the cache facts of one worker: HF_HUB_CACHE, WORKER_UID, and each mount other than /models.
	render() {  # ENV_FILE PROFILE SERVICE
		env -i PATH=/usr/bin:/bin HOME="$case_dir" "$real_docker" compose -p hfcache-render \
			--env-file "$1" -f "$repo/deploy/compose/compose.yml" --profile "$2" config --format json 2>/dev/null |
			python3 -c '
import json, sys
service = json.load(sys.stdin)["services"][sys.argv[1]]
env = service.get("environment") or {}
mounts = [[m.get("type"), m.get("source"), m.get("target")] for m in service.get("volumes", []) if m.get("target") != "/models"]
print(json.dumps({"cache": env.get("HF_HUB_CACHE"), "mounts": mounts, "uid": env.get("WORKER_UID") or "", "gid": env.get("WORKER_GID") or ""}, separators=(",", ":")))
' "$3"
	}
	expect_default='{"cache":"/root/.cache/huggingface/hub","mounts":[["volume","hf-cache","/root/.cache/huggingface"]],"uid":"","gid":""}'
	expect_shared="{\"cache\":\"$case_dir/hub\",\"mounts\":[[\"bind\",\"$case_dir/hub\",\"$case_dir/hub\"]],\"uid\":\"1000\",\"gid\":\"1000\"}"
	for pair in gpu:worker rocm:worker-rocm; do
		profile="${pair%%:*}"
		service="${pair#*:}"
		got_default="$(render "$case_dir/render-default.env" "$profile" "$service" 2>&1)"
		got_shared="$(render "$case_dir/render-shared.env" "$profile" "$service" 2>&1)"
		if [[ "$got_default" == "$expect_default" && "$got_shared" == "$expect_shared" ]]; then
			pass_case "compose-render-$service"
		else
			fail_case "compose-render-$service" "default=$got_default shared=$got_shared"
		fi
	done
else
	printf 'skip compose render: docker compose is not installed\n'
fi

if ((failures > 0)); then
	printf '%d host cache check(s) failed\n' "$failures"
	exit 1
fi
printf 'host cache checks ok\n'
