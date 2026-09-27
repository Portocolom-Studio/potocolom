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
GPU execution, asset-file backup, or a previous released worker image.

Trivy blocks publication when it finds a HIGH or CRITICAL vulnerability with
an available fix. Scan reports are retained. Vulnerabilities without an available patch
still need release review; a green scan does not prove that an image is safe.

After all checks pass, the workflow reserves the version with a draft GitHub
release before pushing any image. An existing draft or published release stops
a rerun before it can overwrite an image. The image names are
`ghcr.io/portocolom-studio/potocolom-api:vMAJOR.MINOR.PATCH` and
`ghcr.io/portocolom-studio/potocolom-worker:vMAJOR.MINOR.PATCH-cuda` or `-rocm`. It attaches image digests, scan reports,
and the static site extracted from the tested API image with its SHA-256 hash.
It does not deploy a service or update a `latest` tag. Image publication can
partly succeed if the registry fails; the reserved draft then needs manual
repair or a new version. Do not delete the reservation, move a version tag or
reuse a version. Publish the draft only after all attachments and checks exist.

Before publishing the draft:

- Confirm the full test and CPU inference results for this commit.
- Run one real image and one canvas session on CUDA and on ROCm. Check image
  output, cancellation, worker restart and continued frames. Keep GPU memory
  below the display adapter's available memory.
- Run the previous release's worker against the new API when an earlier
  release exists. The first release has no earlier artifact to test.
- Check sign-in over HTTPS, the `__Host-` cookies, TOTP, invitations, session
  revocation and logout on the deployed profile.
- Restore the installation's database and asset files together in an isolated
  environment, then open a saved image. The SQL-only CI restore is not an
  installation backup test.
- Review scan results and write release notes for users. Publish the draft
  only when these checks pass.

The workflow needs Docker, Python 3.11, Node 24 and Chrome on the trusted
self-hosted runner. Both GPU base images are large; check free disk space
before creating the tag. No GPU device is passed to CI containers.
