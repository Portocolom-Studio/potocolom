# Sourced by the installer test suites. They run the real installer locks, and
# every case uses the default project, so two suites at once take each other's
# project lock and fail with "another potocolom install" (#831). This runs the
# suite again under flock, so it waits until no other installer suite of this
# user runs. flock -o closes the lock before the suite starts: only the flock
# process holds it, so a background child the suite leaves behind never does,
# and the lock is released when the suite exits. A suite started by a suite
# that already holds the lock does not wait for it.
# shellcheck source=scripts/install-lock.sh
source "$(dirname "${BASH_SOURCE[0]}")/install-lock.sh"
# POTOCOLOM_SUITE_LOCK_NAME lets scripts/test-install-serial-lock.sh use a lock
# of its own, so a real suite running at the same time cannot slow it down.
_suite_lock="$(install_lock_key suites "${POTOCOLOM_SUITE_LOCK_NAME:-installer}")" || exit 1
if [[ "${POTOCOLOM_SUITE_LOCK_HELD:-}" != "$_suite_lock" ]]; then
	exec env POTOCOLOM_SUITE_LOCK_HELD="$_suite_lock" flock -o "$_suite_lock" bash "$0" "$@"
fi
