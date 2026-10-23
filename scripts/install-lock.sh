#!/usr/bin/env bash

# Locks use per-user files under a private directory. flock releases ownership
# when the process exits, so leftover regular files do not leave stale locks.
install_lock_key() {
	local kind="$1" value="$2" checksum lock_dir
	checksum="$(printf '%s' "$value" | cksum | awk '{print $1}')"
	lock_dir="$(install_lock_directory)" || return 1
	printf '%s/%s-%s.lock\n' "$lock_dir" "$kind" "$checksum"
}

install_lock_directory() {
	local dir="/tmp/potocolom-locks-$(id -u)" mode
	if [[ -L "$dir" ]]; then
		echo 'error: unsafe installer lock directory: it is a symbolic link' >&2
		return 1
	fi
	if [[ ! -e "$dir" ]]; then
		(umask 077; mkdir -m 700 -- "$dir") 2>/dev/null || true
	fi
	if [[ ! -d "$dir" || -L "$dir" || ! -O "$dir" ]]; then
		echo 'error: unsafe installer lock directory ownership or type' >&2
		return 1
	fi
	mode="$(stat -c '%a' "$dir")" || return 1
	if [[ "$mode" != 700 ]]; then
		echo 'error: installer lock directory must have mode 0700' >&2
		return 1
	fi
	printf '%s\n' "$dir"
}

install_lock_path_key() {
	local dir="$1" canonical
	[[ "$dir" == /* ]] || dir="$PWD/$dir"
	canonical="$(readlink -m -- "$dir")" || return 1
	install_lock_key path "$canonical"
}

install_compose_value() {
	local env_file="$1" compose_file="$2" key="$3" output
	if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
		output="$(unset "$key" COMPOSE_PROJECT_NAME; docker compose --env-file "$env_file" -f "$compose_file" config --environment 2>/dev/null)" || return 2
		printf '%s\n' "$output" | awk -F= -v key="$key" '$1 == key { value = substr($0, index($0, "=") + 1); found = 1 } END { if (found) print value }'
		return 0
	fi
	# Direct guard runs may have no Docker CLI. Read common dotenv assignments
	# without evaluating shell syntax; fail on values that need Compose expansion.
	awk -v key="$key" '
		{
			sub(/\r$/, "")
			line = $0
			sub(/^[[:space:]]*/, "", line)
			if (line ~ /^export[[:space:]]+/) sub(/^export[[:space:]]+/, "", line)
			if (line !~ ("^" key "([[:space:]:=]|$)")) next
			if (line !~ ("^" key "[[:space:]]*[:=]")) { uncertain = 1; next }
			sub("^" key "[[:space:]]*[:=][[:space:]]*", "", line)
			if (line ~ /^\"/) {
				if (line !~ /^\"([^\"\\]|\\.)*\"([[:space:]]*#.*)?$/) { uncertain = 1; next }
				sub(/^\"/, "", line)
				sub(/\"([[:space:]]*#.*)?$/, "", line)
			} else if (line ~ /^\047/) {
				if (line !~ /^\047[^\047]*\047([[:space:]]*#.*)?$/) { uncertain = 1; next }
				sub(/^\047/, "", line)
				sub(/\047([[:space:]]*#.*)?$/, "", line)
			} else {
				sub(/[[:space:]]+#.*$/, "", line)
			}
			sub(/[[:space:]]*$/, "", line)
			if (line ~ /\$/) { uncertain = 1; next }
			value = line
			found = 1
		}
		END { if (uncertain) exit 2; if (found) print value }
	' "$env_file" 2>/dev/null
}

install_compose_project() {
	local env_file="$1" compose_file="$2" value
	if [[ ! -f "$env_file" ]] || ! awk '
		{ line = $0; sub(/\r$/, "", line) }
		line ~ /^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROJECT_NAME[[:space:]]*[:=]/ { found = 1 }
		END { exit !found }
	' "$env_file"; then
		printf 'potocolom\n'
		return 0
	fi
	value="$(install_compose_value "$env_file" "$compose_file" COMPOSE_PROJECT_NAME)" || return 1
	printf '%s\n' "${value:-potocolom}"
}

install_acquire_locks() {
	local dir="$1" env_file="$2" compose_file="$3" project="$4"
	local path_lock project_lock item fd old_umask inherited_fd inherited_key inherited_target
	if ! command -v flock >/dev/null 2>&1; then
		echo 'error: flock is required for safe installs (install util-linux, then retry)' >&2
		exit 1
	fi
	if ! command -v cksum >/dev/null 2>&1; then
		echo 'error: cksum is required for safe installs (install coreutils, then retry)' >&2
		exit 1
	fi
	if ! command -v stat >/dev/null 2>&1 || ! command -v readlink >/dev/null 2>&1; then
		echo 'error: stat and readlink are required for safe installer locks (install coreutils, then retry)' >&2
		exit 1
	fi
	path_lock="$(install_lock_path_key "$dir")"
	project_lock="$(install_lock_key project "$project")"
	local locks=()
	if [[ "$path_lock" < "$project_lock" ]]; then locks=("$path_lock" "$project_lock"); else locks=("$project_lock" "$path_lock"); fi
	for item in "${locks[@]}"; do
		inherited_fd="${POTOCOLOM_PROJECT_LOCK_FD:-}"
		inherited_key="${POTOCOLOM_PROJECT_LOCK_KEY:-}"
		[[ "$item" != "$path_lock" ]] || { inherited_fd="${POTOCOLOM_PATH_LOCK_FD:-}"; inherited_key="${POTOCOLOM_PATH_LOCK_KEY:-}"; }
		if [[ "$inherited_key" == "$item" && "$inherited_fd" =~ ^[0-9]+$ \
			&& -e "/proc/$$/fd/$inherited_fd" ]]; then
			inherited_target="$(readlink "/proc/$$/fd/$inherited_fd")"
			if [[ "$inherited_target" == "$item" && -f "$item" && ! -L "$item" && -O "$item" \
				&& "$(stat -c '%a' "$item")" == 600 ]] && flock -n "$inherited_fd" 2>/dev/null; then
				continue
			fi
		fi
		if [[ -L "$item" || ( -e "$item" && ( ! -f "$item" || ! -O "$item" ) ) ]]; then
			echo 'error: unsafe installer lock file; refusing to open it' >&2
			exit 1
		fi
		if [[ ! -e "$item" ]]; then
			old_umask="$(umask)"
			umask 077
			( set -o noclobber; : >"$item" ) 2>/dev/null || true
			umask "$old_umask"
		fi
		if [[ -L "$item" || ! -f "$item" || ! -O "$item" ]]; then
			echo 'error: unsafe installer lock file; refusing to open it' >&2
			exit 1
		fi
		if [[ "$(stat -c '%a' "$item")" != 600 ]]; then
			echo 'error: installer lock file must have mode 0600' >&2
			exit 1
		fi
		old_umask="$(umask)"
		umask 077
		exec {fd}>>"$item"
		umask "$old_umask"
		if ! flock -n "$fd"; then
			echo 'error: another potocolom install or preflight is using this directory or Compose project.' >&2
			echo '  Wait for it to finish, then run this command again.' >&2
			exit 1
		fi
		if [[ "$item" == "$path_lock" ]]; then
			POTOCOLOM_PATH_LOCK_FD="$fd"
			POTOCOLOM_PATH_LOCK_KEY="$path_lock"
			export POTOCOLOM_PATH_LOCK_FD POTOCOLOM_PATH_LOCK_KEY
		else
			POTOCOLOM_PROJECT_LOCK_FD="$fd"
			POTOCOLOM_PROJECT_LOCK_KEY="$project_lock"
			export POTOCOLOM_PROJECT_LOCK_FD POTOCOLOM_PROJECT_LOCK_KEY
		fi
	done
	:
}
