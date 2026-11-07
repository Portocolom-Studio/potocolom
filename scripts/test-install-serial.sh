# Sourced by the installer test suites. They run the real installer locks, and
# every case uses the default project, so two suites at once take each other's
# project lock and fail with "another potocolom install" (#831). This waits until
# no other installer suite of this user runs. The lock file sits in the private
# per-user lock directory that install-lock.sh checks, and flock releases it
# when the suite exits.
# shellcheck source=scripts/install-lock.sh
source "$(dirname "${BASH_SOURCE[0]}")/install-lock.sh"
_suite_lock="$(install_lock_key suites installer)" || exit 1
exec {_suite_fd}>>"$_suite_lock"
flock "$_suite_fd"
