#!/bin/sh
# Starts the worker as root, or as the host cache owner when one is shared.
#
# WORKER_UID unset: exec the command as root, the default install.
# WORKER_UID set (scripts/install.sh POTOCOLOM_HF_CACHE, or a hand-edited
# .env): make /models, and STATE_DIR when it is set, owned by that uid when it
# is not already, then drop to WORKER_UID with the groups that own the GPU
# device nodes plus WORKER_GID. The shared Hugging Face cache is never chowned
# here; the installer checks its owner before it is shared, and this script
# refuses a cache that overlaps a path it chowns.
set -eu

if [ -z "${WORKER_UID:-}" ]; then
	exec "$@"
fi

uid="$WORKER_UID"
gid="${WORKER_GID:-}"
models_dir=/models
dev_dir=/dev
home_dir=/tmp/worker-home

case "$uid" in '' | *[!0-9]*) echo "error: WORKER_UID must be a number" >&2; exit 1 ;; esac
case "$gid" in '' | *[!0-9]*) echo "error: WORKER_GID must be a number when WORKER_UID is set" >&2; exit 1 ;; esac

# The joined worker keeps its credential in STATE_DIR, a volume it must write.
state_dir="${STATE_DIR:-}"
if [ -n "$state_dir" ]; then
	case "$state_dir" in /*) ;; *) echo "error: STATE_DIR must be an absolute path" >&2; exit 1 ;; esac
	state_dir="$(readlink -m -- "$state_dir")"
fi

# True when one of two absolute paths is the other or lies inside it.
overlaps() {  # PATH_A PATH_B
	case "$2/" in "${1%/}/"*) return 0 ;; esac
	case "$1/" in "${2%/}/"*) return 0 ;; esac
	return 1
}

# STATE_DIR is chowned recursively, so it must hold nothing but the worker's
# state: not /models, not the home directory, and not a directory above them.
if [ -n "$state_dir" ]; then
	for owned in "$models_dir" "$home_dir"; do
		if overlaps "$state_dir" "$owned"; then
			echo "error: STATE_DIR $state_dir is, or is inside or above, $owned; it must lie outside $models_dir and $home_dir" >&2
			exit 1
		fi
	done
fi

# The host cache is mounted at HF_HUB_CACHE. A chown -R of a path that holds it,
# or of a path inside it, would change the cache, so refuse before any chown.
if [ -n "${HF_HUB_CACHE:-}" ]; then
	hub="$(readlink -m -- "$HF_HUB_CACHE")"
	for owned in "$models_dir" "$home_dir" "$state_dir"; do
		[ -n "$owned" ] || continue
		if overlaps "$hub" "$owned"; then
			echo "error: HF_HUB_CACHE $hub is, or is inside or above, $owned; the host cache must lie outside $models_dir, $home_dir and STATE_DIR" >&2
			exit 1
		fi
	done
fi

# Only a volume that a root worker wrote is changed, and only /models and STATE_DIR.
if find "$models_dir" ! -uid "$uid" -print -quit | grep -q .; then
	chown -R "$uid:$gid" "$models_dir"
fi
if [ -n "$state_dir" ] && find "$state_dir" ! -uid "$uid" -print -quit | grep -q .; then
	chown -R "$uid:$gid" "$state_dir"
fi

# Device nodes keep their file permissions: the worker needs the group that
# owns each one it cannot already open. Directories (/dev/dri/by-path) and
# nodes every user can read and write (the NVIDIA default, 0666 root:root) add
# no group, so the root group is never added for them. The list starts with
# WORKER_GID, so it is never empty.
groups="$gid"
for node in "$dev_dir"/kfd "$dev_dir"/dri/* "$dev_dir"/nvidia*; do
	[ -e "$node" ] && [ ! -d "$node" ] || continue
	mode_gid="$(stat -L -c '%a %g' "$node")"
	case "${mode_gid% *}" in *[67]) continue ;; esac
	node_gid="${mode_gid#* }"
	case ",$groups," in
		*",$node_gid,"*) ;;
		*) groups="$groups,$node_gid" ;;
	esac
done

# Torch inductor and the Hugging Face client look up the user name and the
# home directory, and the uid has no passwd entry in the image.
mkdir -p "$home_dir/hf" "$home_dir/inductor"
chown -R "$uid:$gid" "$home_dir"
HOME="$home_dir"
USER=worker
LOGNAME=worker
HF_HOME="$home_dir/hf"
TORCHINDUCTOR_CACHE_DIR="$home_dir/inductor"
export HOME USER LOGNAME HF_HOME TORCHINDUCTOR_CACHE_DIR

exec setpriv --reuid "$uid" --regid "$gid" --groups "$groups" -- "$@"
