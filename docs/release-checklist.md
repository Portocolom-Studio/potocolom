# Release checks

The `release` workflow accepts a `vMAJOR.MINOR.PATCH` tag whose commit is on
`main`. The maintainer creates the tag after review. The workflow runs the full
test gate and the real CPU inference gate, then builds the API, CUDA worker and
ROCm worker images. It requires the CPU inference target from issue #510.

Each exact worker image must import its inference libraries, run a simulated
generation and return realtime frames through the API before and after a
worker is killed. The browser must see `interrupted`, then `resumed`, and keep
the same session ID. The same stack tests a
populated schema upgrade from revision 0023 to head, then a PostgreSQL dump and
restore. These checks use temporary containers and volumes. They do not test
GPU execution. A separate installer gate tests the real AMD worker and the
previous published release.

Trivy blocks publication when it finds a HIGH or CRITICAL vulnerability with
an available fix. Scan reports are retained. Vulnerabilities without an available patch
still need release review; a green scan does not prove that an image is safe.

Before publication, the workflow builds the exact installer, self-host bundle,
and SHA256SUMS. It runs `scripts/release-check.py` on the trusted AMD runner
under `/tmp/potocolom-release-check-gpu.lock`. The check installs those local
artifacts, generates a 768 by 768 PNG, then installs the previous published
release in the same test project. The previous release uses only the test model
and saves a 768 by 768 image. Limiting the old warmup prevents another resident
model from taking the memory needed by its untiled decoder. Before the upgrade, the check restores every model
manifest. The candidate uses all default models and generates at 768 by 768
both before and after the upgrade. It checks the saved bytes and settings.
The installer lists the required images with `docker compose config --images`
and pulls only if an image is missing. This works with older Compose v2 and
lets the check use the exact locally built images before they reach GHCR.

The check refuses an occupied port 8080 and accounts mode without credentials.
It creates a random Compose project and removes only that project's test data.
Model caches stay between phases. On AMD, it stops the owned worker if free
VRAM falls below `VRAM_RESERVE_MB` or disk group offload starts. Private logs
remain on the host after a failed check. A failed check blocks publication.
The monitor covers installer startup, warmup, readiness, generation, and cleanup.
It reads the reserve and selected GPU from the running worker. The worker GPU
is matched to host VRAM counters by PCI address, with one cached match per
container ID. A small secondary card does not count against that GPU. Missing
or ambiguous matches fail the check. The monitor stops workers by their
verified Docker labels. Replacing installer files does not interrupt the monitor.
An unsafe worker stops even before it connects to the API. Cancellation joins
installer child processes. Fresh checks record their project in a private claim
file. A separate workflow cleanup step runs even after cancellation and removes
only resources with that project label. It stops containers before removal so
the live monitor can finish its probes. Cleanup does not need intact installer files.
Checks with `--keep` preserve the install and its data. New PNGs must contain every declared RGB or RGBA row
with valid filters. A valid header alone does not pass.

After all checks pass, the workflow reserves the version with a draft GitHub
release before pushing any image. An existing draft or published release stops
a rerun before it can overwrite an image. The image names are
`ghcr.io/portocolom-studio/potocolom-api:vMAJOR.MINOR.PATCH` and
`ghcr.io/portocolom-studio/potocolom-worker:vMAJOR.MINOR.PATCH-cuda` or `-rocm`. It attaches image digests, scan reports,
the static site extracted from the tested API image with its SHA-256 hash, the
self-host bundle (`potocolom-vMAJOR.MINOR.PATCH-selfhost.tar.gz`) and
`install.sh`, the script a self-hoster curls to install the release without a
clone. It does not deploy a service or update a `latest` tag. Image publication can
partly succeed if the registry fails; the reserved draft then needs manual
repair or a new version. Do not delete the reservation, move a version tag or
reuse a version. Publish the draft only after all attachments and checks exist.

Before publishing the draft:

- Confirm the backend, worker and frontend versions match the tag (the verify
  job refuses a mismatch); bump with `npm version X.Y.Z --no-git-tag-version`
  in frontend/ and the `version =` line of both pyproject files.
- Confirm the full test and CPU inference results for this commit.
- Run one real image and one canvas session on CUDA and on ROCm. Check image
  output, cancellation, worker restart and continued frames. Keep GPU memory
  below the display adapter's available memory.
- Run the previous release's worker against the new API when an earlier
  release exists. The first release has no earlier artifact to test.
- Check sign-in over HTTPS, the `__Host-` cookies, TOTP, invitations, session
  revocation and logout on the deployed profile.
- Restore the installation's database and asset files together in an isolated
  environment, then open a saved image. `scripts/test-backup-restore.sh` runs
  this proof automatically in this workflow and
  [docs/self-hosting.md](self-hosting.md#backup-and-restore) documents
  `scripts/backup.sh` and `scripts/restore.sh`; run the two commands once by
  hand on the deployed profile as well. The SQL-only CI restore is not an
  installation backup test.
- Review scan results and write release notes for users. Publish the draft
  only when these checks pass.

After publishing: on the first release, set the three GHCR packages to public
in the organization's package settings, then run install.sh from the published
release in an empty POTOCOLOM_DIR.

The workflow needs Docker, Python 3.11, Node 24 and Chrome on the trusted
self-hosted runner. Both GPU base images are large; check free disk space
before creating the tag. Pull request jobs use simulated or CPU workers.
The release installer gate requires the AMD GPU, `/dev/kfd`, `/dev/dri`, and
free host port 8080. It runs only on a version tag.

## NVIDIA check over SSH

`make release-check-nvidia` downloads the release artifacts and runs the same
check driver on an SSH host with key-based login, Docker, and the NVIDIA Container Toolkit.
The default install directory is the remote `~/potocolom`.

```sh
make release-check-nvidia HOST=leon@192.168.1.159 VERSION=v0.1.3
```

`INSTALL_DIR=/absolute/path` selects another remote install. `RELEASE_DIR` selects
a local directory with `install.sh`, the self-host bundle, and `SHA256SUMS`.
This option tests a candidate before a draft exists. The wrapper verifies the
checksums and sends only those artifacts and the driver. GitHub credentials
stay on the local host.

The remote check keeps the install and its volumes, including on failure. An
existing install must own port 8080 and use `AUTH_MODE=none`. The check hashes
each saved asset before the upgrade, checks its bytes and the existing settings
after the upgrade, then generates a 768 by 768 PNG. An empty or absent install
directory starts a fresh stack and keeps it. The command reports `PASS` only
after generation succeeds. A failed check needs review before publication.
