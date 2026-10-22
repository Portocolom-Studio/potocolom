#!/usr/bin/env python3
"""Exercise a released installer against an owned real GPU/ROCm stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import (HTTPRedirectHandler, ProxyHandler, Request,
                            build_opener)


MODEL = "ssd-1b-lightning"
TERMINAL = {"failed", "cancelled"}


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        old, new = urlsplit(request.full_url), urlsplit(newurl)
        try:
            same_origin = url_origin(old) == url_origin(new)
        except ValueError:
            same_origin = False
        if not same_origin:
            raise HTTPError(newurl, code, "cross-origin redirect refused", headers, file)
        return super().redirect_request(request, file, code, message, headers, newurl)


def url_origin(parts) -> tuple[str, str, int | None]:
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parts.hostname or "").lower(), port


urlopen = build_opener(ProxyHandler({}), SameOriginRedirect()).open
SAFE_ENV_KEYS = {"PATH", "HOME", "TMPDIR", "XDG_RUNTIME_DIR", "DOCKER_CONFIG",
                 "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY",
                 "DOCKER_CERT_PATH", "DOCKER_API_VERSION"}


class CheckError(RuntimeError):
    pass


def safe_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS}


def run(args: list[str], *, env: dict[str, str] | None = None,
        timeout: float = 1800, log: Path | None = None) -> str:
    """Run quietly to a private file when requested; expose only safe errors."""
    process = None
    try:
        process = subprocess.Popen(args, env=env or safe_environment(), text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=0.5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            output = process.communicate()[0]
        else:
            output = exc.stdout or ""
        if log:
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(output, encoding="utf-8")
            log.chmod(0o600)
        raise CheckError(f"{Path(args[0]).name} timed out; private log: {log or 'unavailable'}") from exc
    except OSError as exc:
        raise CheckError(f"command failed to start: {Path(args[0]).name}") from exc
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(output, encoding="utf-8")
        log.chmod(0o600)
    if process.returncode:
        raise CheckError(f"{Path(args[0]).name} exited {process.returncode}; private log: {log or 'unavailable'}")
    return output


def verify_release(directory: Path, version: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise CheckError("version must be vX.Y.Z")
    installer = directory / "install.sh"
    bundle = directory / f"potocolom-{version}-selfhost.tar.gz"
    sums = directory / "SHA256SUMS"
    if not all(p.is_file() for p in (installer, bundle, sums)):
        raise CheckError(f"release directory must contain install.sh, {bundle.name}, and SHA256SUMS")
    lines = sums.read_text(encoding="utf-8").splitlines()
    for artifact in (installer, bundle):
        match = next((line.split()[0] for line in lines
                      if re.search(rf"  {re.escape(artifact.name)}$", line)), None)
        if match is None or not re.fullmatch(r"[0-9a-fA-F]{64}", match):
            raise CheckError(f"SHA256SUMS has no valid entry for {artifact.name}")
        hasher = hashlib.sha256()
        with artifact.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        if digest.lower() != match.lower():
            raise CheckError(f"SHA256SUMS does not verify {artifact.name}")
    return installer, bundle


def run_installer(installer: Path, version: str, install_dir: Path,
                  release_dir: Path | None, profile: str, log_dir: Path) -> None:
    env = safe_environment()
    env.update(POTOCOLOM_VERSION=version, POTOCOLOM_DIR=str(install_dir),
               POTOCOLOM_PROFILE=profile, DOCKER_BUILDKIT="0")
    if release_dir is not None:
        env["POTOCOLOM_RELEASE_URL"] = release_dir.resolve().as_uri()
    log = log_dir / f"installer-{version}-{int(time.time())}.log"
    run(["bash", str(installer)], env=env, timeout=3600, log=log)
    if parse_env(install_dir / "deploy/compose/.env").get("POTOCOLOM_VERSION") != version:
        raise CheckError("installer did not pin the requested release")


def http_json(base: str, path: str, *, method: str = "GET",
              body: dict | None = None, timeout: int = 15) -> object:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(urljoin(base.rstrip("/") + "/", path.lstrip("/")), data=data,
                      method=method, headers={"Accept": "application/json",
                      "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status in (401, 403):
                raise CheckError("API requires accounts authentication; no auth was supplied")
            return json.loads(response.read())
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise CheckError("API requires accounts authentication; no auth was supplied") from exc
        raise CheckError(f"API returned HTTP {exc.code} for {path}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CheckError(f"API request failed for {path}") from exc


def api_base(install_dir: Path) -> str:
    compose = install_dir / "deploy/compose/compose.yml"
    env_file = install_dir / "deploy/compose/.env"
    if not compose.is_file() or not env_file.is_file():
        raise CheckError("install requires deploy/compose/.env and compose.yml")
    values = compose_environment(env_file, compose)
    if values.get("AUTH_MODE", "none") != "none":
        raise CheckError("accounts mode requires supplied auth; refusing before install changes")
    public = values.get("PUBLIC_URL", "http://localhost:8080")
    try:
        parts = urlsplit(public)
        hostname = parts.hostname or ""
        parts.port  # Validate the port even when the origin uses its default.
        valid = (parts.scheme in {"http", "https"} and bool(parts.hostname)
                 and re.fullmatch(r"[A-Za-z0-9.-]+|[0-9A-Fa-f:]+", hostname) is not None
                 and parts.username is None and parts.password is None
                 and not parts.netloc.endswith(":") and parts.path in {"", "/"}
                 and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise CheckError("PUBLIC_URL must be an HTTP(S) origin without credentials")
    return public.rstrip("/")


def compose_environment(env_file: Path, compose_file: Path) -> dict[str, str]:
    """Ask Compose to evaluate its own interpolation rules without starting services."""
    output = run(["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file),
                  "config", "--environment"], timeout=30)
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value
    return values


def parse_env(path: Path) -> dict[str, str]:
    """Return Compose's evaluated .env values, without exposing command output."""
    return compose_environment(path, path.with_name("compose.yml"))


def assert_env_preserved(before: dict[str, str], after: dict[str, str]) -> None:
    for key, value in before.items():
        if key == "POTOCOLOM_VERSION" or (key == "COMPOSE_PROJECT_NAME" and not value):
            continue
        if after.get(key) != value:
            raise CheckError(f"upgrade changed existing .env key {key}")


def asset_url(base: str, item: dict) -> str:
    value = item.get("download_url") or item.get("url")
    if not isinstance(value, str):
        raise CheckError("generation returned no asset URL")
    try:
        absolute = urljoin(base.rstrip("/") + "/", value)
        base_parts, asset_parts = urlsplit(base), urlsplit(absolute)
        same_origin = url_origin(asset_parts) == url_origin(base_parts)
    except ValueError:
        same_origin = False
    if not same_origin:
        raise CheckError("API returned a non-local asset URL")
    return absolute


def asset_digest(base: str, item: dict, *, limit: int = 512 * 1024 * 1024) -> str:
    request = Request(asset_url(base, item), headers={"Accept": "image/*"})
    try:
        with urlopen(request, timeout=30) as response:
            digest = hashlib.sha256()
            total = 0
            deadline = time.monotonic() + 120
            while chunk := response.read(64 * 1024):
                if time.monotonic() >= deadline:
                    raise CheckError("saved asset download exceeded its deadline")
                total += len(chunk)
                if total > limit:
                    raise CheckError("saved asset exceeds the 512 MiB download bound")
                digest.update(chunk)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CheckError("asset download failed") from exc
    if total == 0:
        raise CheckError("saved asset is empty")
    return digest.hexdigest()


def local_asset(base: str, item: dict) -> bytes:
    """Download and validate only the newly generated test image."""
    request = Request(asset_url(base, item), headers={"Accept": "image/png"})
    try:
        with urlopen(request, timeout=30) as response:
            if response.headers.get_content_type() != "image/png":
                raise CheckError("asset response is not a non-empty PNG")
            data = response.read(64 * 1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CheckError("asset download failed") from exc
    if len(data) > 64 * 1024 * 1024:
        raise CheckError("asset response is not a non-empty PNG")
    width, height = validate_png(data)
    if (width, height) != (768, 768):
        raise CheckError(f"PNG has unexpected dimensions {width}x{height}")
    return data


def validate_png(data: bytes) -> tuple[int, int]:
    if len(data) < 45 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CheckError("asset response is not a non-empty PNG")
    offset = 8
    dimensions: tuple[int, int] | None = None
    image_data = bytearray()
    ended = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            break
        payload = data[offset + 8:offset + 8 + length]
        crc = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            break
        if kind == b"IHDR" and offset == 8 and length == 13:
            dimensions = (int.from_bytes(payload[:4], "big"), int.from_bytes(payload[4:8], "big"))
        elif kind == b"IDAT":
            image_data.extend(payload)
        elif kind == b"IEND" and length == 0:
            ended = end == len(data)
            break
        offset = end
    if dimensions is None or not image_data or not ended:
        raise CheckError("asset response is not a complete PNG")
    if dimensions != (768, 768):
        return dimensions
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(image_data, 768 * 768 * 8 + 768 + 1)
        if not decoded or not decoder.eof or decoder.unconsumed_tail:
            raise CheckError("PNG image data is empty")
    except zlib.error as exc:
        raise CheckError("PNG image data is invalid") from exc
    return dimensions


def generate(base: str, prompt: str, safety_check=None) -> tuple[str, bytes]:
    worker_deadline = time.monotonic() + 1800
    while True:
        models = http_json(base, "/api/v1/models")
        entries = models.get("models", models) if isinstance(models, dict) else models
        if isinstance(entries, list) and any(
                isinstance(m, dict) and m.get("id") == MODEL for m in entries):
            break
        if time.monotonic() >= worker_deadline:
            raise CheckError(f"no real worker offered {MODEL} within 30 minutes")
        time.sleep(5)
    made = http_json(base, "/api/v1/generations", method="POST", body={
        "model_id": MODEL,
        "params": {"prompt": prompt, "width": 768, "height": 768,
                    "steps": 4, "guidance": 0, "seed": 42},
    })
    if not isinstance(made, dict) or not isinstance(made.get("job_id"), str):
        raise CheckError("API did not return a job_id")
    job_id = made["job_id"]
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        if safety_check is not None:
            safety_check()
        job = http_json(base, f"/api/v1/generations/{quote(job_id, safe='')}")
        if not isinstance(job, dict):
            raise CheckError("API returned an invalid generation")
        state = job.get("state")
        if state in TERMINAL:
            raise CheckError(f"generation {state}")
        if state == "succeeded":
            assets = job.get("assets")
            if not isinstance(assets, list) or not assets or not isinstance(assets[0], dict):
                raise CheckError("successful generation returned no assets")
            asset_id = assets[0].get("id")
            if not isinstance(asset_id, str) or not asset_id:
                raise CheckError("successful generation returned no asset ID")
            return asset_id, local_asset(base, assets[0])
        time.sleep(2)
    raise CheckError("generation timed out after 30 minutes")


def snapshot_assets(base: str) -> dict[str, str]:
    cursor = ""
    found: dict[str, str] = {}
    while True:
        path = "/api/v1/generations?limit=200" + (f"&cursor={quote(cursor)}" if cursor else "")
        page = http_json(base, path)
        if not isinstance(page, list):
            raise CheckError("generation listing did not return a list")
        if not page:
            return found
        for job in page:
            if not isinstance(job, dict):
                continue
            for asset in job.get("assets", []):
                if isinstance(asset, dict) and isinstance(asset.get("id"), str):
                    found[asset["id"]] = asset_digest(base, asset)
        cursor = str(page[-1].get("id", ""))
        if not cursor:
            raise CheckError("generation page has no cursor ID")


def verify_assets(base: str, expected: dict[str, str]) -> None:
    current = snapshot_assets(base)
    for asset_id, digest in expected.items():
        if current.get(asset_id) != digest:
            raise CheckError(f"saved asset missing or changed: {asset_id}")


def free_port_8080() -> None:
    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=1):
            raise CheckError("host port 8080 is occupied; refusing to stop another stack")
    except OSError:
        return


def compose_owned(install_dir: Path, project: str, args: list[str]) -> None:
    compose = install_dir / "deploy/compose/compose.yml"
    env_file = install_dir / "deploy/compose/.env"
    run(["docker", "compose", "-p", project, "--env-file", str(env_file),
         "-f", str(compose), "--profile", "*", *args], timeout=300)


def bundle_env_example(release_dir: Path, version: str) -> str:
    _, bundle = verify_release(release_dir, version)
    wanted = "deploy/compose/.env.example"
    try:
        with tarfile.open(bundle, "r:gz") as archive:
            member = next((item for item in archive.getmembers() if item.name == wanted), None)
            if member is None or not member.isfile() or member.size > 128 * 1024:
                raise CheckError("release bundle has no safe deploy/compose/.env.example")
            stream = archive.extractfile(member)
            if stream is None:
                raise CheckError("release bundle has no readable .env.example")
            return stream.read(128 * 1024 + 1).decode("utf-8")
    except (tarfile.TarError, UnicodeError, OSError) as exc:
        raise CheckError("cannot read .env.example from verified release bundle") from exc


def prepare_fresh(directory: Path, project: str, release_dir: Path, version: str) -> None:
    if directory.exists():
        if not directory.is_dir() or any(directory.iterdir()):
            raise CheckError(f"fresh install directory must be empty: {directory}")
    else:
        directory.mkdir(parents=True)
    compose = directory / "deploy/compose"
    compose.mkdir(parents=True, exist_ok=True)
    template = bundle_env_example(release_dir, version)
    values = {"COMPOSE_PROJECT_NAME": project, "POSTGRES_PASSWORD": secrets.token_hex(32),
              "FLEET_SECRET": secrets.token_hex(32), "AUTH_MODE": "none"}
    lines = [line for line in template.splitlines() if not any(line.startswith(k + "=") for k in values)]
    lines.extend(f"{k}={v}" for k, v in values.items())
    (compose / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (compose / ".env").chmod(0o600)


def fresh_rehearsal(args: argparse.Namespace, candidate: Path) -> None:
    previous_installer, _ = verify_release(args.previous_release_dir, args.previous_version)
    free_port_8080()
    stamp = secrets.token_hex(8)
    candidate_dir = args.install_dir
    project = f"release-check-{stamp}"
    owned: list[tuple[Path, str]] = []
    logs = Path(tempfile.mkdtemp(prefix="release-check-logs-"))
    passed = False
    prepared = False
    safety_check = (lambda: rocm_safety_check(candidate_dir, project, logs)) if args.profile == "rocm" else None
    try:
        if candidate_dir.exists() and (not candidate_dir.is_dir() or any(candidate_dir.iterdir())):
            raise CheckError(f"fresh install directory must be empty: {candidate_dir}")
        prepared = True
        prepare_fresh(candidate_dir, project, args.release_dir, args.version)
        if _project_exists(project):
            raise CheckError(f"owned test project already exists: {project}")
        owned.append((candidate_dir, project))
        run_installer(candidate, args.version, candidate_dir, args.release_dir, args.profile, logs)
        if not project_owns_api_port(project):
            raise CheckError("fresh install does not own API port 8080")
        api = api_base(candidate_dir)
        wait_ready(api)
        generate(api, "release install check fresh generation", safety_check)
        compose_owned(candidate_dir, project, ["down", "--remove-orphans"])
        remove_owned_data_volumes(project)
        run_installer(previous_installer, args.previous_version, candidate_dir,
                      None, args.profile, logs)
        api = api_base(candidate_dir)
        wait_ready(api)
        saved_id, saved = generate(api, "release upgrade check saved image", safety_check)
        env_before = parse_env(candidate_dir / "deploy/compose/.env")
        run_installer(candidate, args.version, candidate_dir, args.release_dir, args.profile, logs)
        env_after = parse_env(candidate_dir / "deploy/compose/.env")
        assert_env_preserved(env_before, env_after)
        if not project_owns_api_port(project):
            raise CheckError("upgraded install does not own API port 8080")
        after = api_base(candidate_dir)
        wait_ready(after)
        verify_assets(after, {saved_id: hashlib.sha256(saved).hexdigest()})
        generate(after, "release upgrade check generation", safety_check)
        passed = True
    finally:
        cleanup_error = None
        failed_cleanup: set[Path] = set()
        for directory, project in owned:
            if _project_exists(project):
                try:
                    compose_owned(directory, project, ["down", "-v", "--remove-orphans"])
                except CheckError as exc:
                    cleanup_error = exc
                    failed_cleanup.add(directory)
                    continue
        for directory, _ in owned:
            if directory not in failed_cleanup:
                shutil.rmtree(directory, ignore_errors=True)
        if prepared and not owned:
            shutil.rmtree(candidate_dir, ignore_errors=True)
        if passed and cleanup_error is None:
            shutil.rmtree(logs, ignore_errors=True)
        else:
            print(f"private installer logs retained at {logs}", file=sys.stderr)
        if cleanup_error is not None:
            raise CheckError(f"owned project cleanup failed: {cleanup_error}") from cleanup_error


def wait_ready(base: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            request = Request(urljoin(base.rstrip("/") + "/", "api/v1/ready"))
            with urlopen(request, timeout=5) as response:
                if response.status == 200 and response.read() == b"":
                    return
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(2)
    raise CheckError("API store did not become ready before the deadline")


def remove_owned_data_volumes(project: str) -> None:
    """Reset this run's database and assets, retaining its model caches."""
    run(["docker", "volume", "rm", f"{project}_pgdata", f"{project}_assets"], timeout=60)


def rocm_vram_usage(sysfs: Path = Path("/sys/class/drm")) -> list[tuple[str, int, int]]:
    """Read physical AMD cards; ignore small APUs that expose only 512 MiB."""
    cards = []
    for device in sorted(sysfs.glob("card[0-9]*/device")):
        try:
            vendor = (device / "vendor").read_text(encoding="ascii").strip().lower()
            total = int((device / "mem_info_vram_total").read_text(encoding="ascii").strip())
            used = int((device / "mem_info_vram_used").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            continue
        if vendor == "0x1002" and total >= 1024 * 1024 * 1024 and 0 <= used <= total:
            cards.append((device.parent.name, total, used))
    return cards


def owned_rocm_worker_ids(project: str) -> list[str]:
    result = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                             "--filter", "label=com.docker.compose.service=worker-rocm",
                             "--format", "{{.ID}}"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=15, env=safe_environment())
    if result.returncode:
        raise CheckError("cannot verify owned ROCm worker")
    return [line for line in result.stdout.splitlines() if re.fullmatch(r"[0-9a-fA-F]{12,64}", line)]


def rocm_safety_check(install_dir: Path, project: str, evidence_dir: Path,
                      sysfs: Path = Path("/sys/class/drm")) -> None:
    compose = install_dir / "deploy/compose/compose.yml"
    env_file = install_dir / "deploy/compose/.env"
    values = compose_environment(env_file, compose)
    try:
        reserve_mb = int(values.get("VRAM_RESERVE_MB", "1536"))
    except ValueError as exc:
        raise CheckError("VRAM_RESERVE_MB must be between 256 and 65536") from exc
    if not 256 <= reserve_mb <= 65536:
        raise CheckError("VRAM_RESERVE_MB must be between 256 and 65536")
    cards = rocm_vram_usage(sysfs)
    if not cards:
        raise CheckError("no physical AMD GPU with reportable VRAM was found")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stats = "\n".join(f"{name}: total={total} used={used} free={total - used}"
                       for name, total, used in cards)
    worker_ids = owned_rocm_worker_ids(project)
    if not worker_ids:
        raise CheckError("cannot verify a running worker in the owned ROCm project")
    worker_log = run(["docker", "compose", "-p", project, "--env-file", str(env_file), "-f", str(compose),
                      "logs", "--tail", "40", "worker-rocm"], timeout=15)
    group_offload = re.search(r"(?:to\s+|rung[=: ]+)group_offload", worker_log, re.IGNORECASE)
    disk_offloads = ""
    for worker_id in worker_ids:
        disk_offloads += run([
            "docker", "exec", worker_id, "sh", "-c",
            "if [ -d /models/.offload ]; then "
            "find /models/.offload -mindepth 1 -maxdepth 1 -type d -print -quit; fi",
        ], timeout=15)
    breached = any(total - used < reserve_mb * 1024 * 1024 for _, total, used in cards)
    if group_offload or disk_offloads.strip() or breached:
        evidence = evidence_dir / "rocm-safety-evidence.log"
        fd = os.open(evidence, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(stats + "\n--- owned worker tail ---\n" + worker_log
                         + "\n--- disk offload paths ---\n" + disk_offloads)
        compose_owned(install_dir, project, ["stop", "--timeout", "0", "worker-rocm"])
        reason = "group offload rung detected" if group_offload or disk_offloads.strip() else "VRAM reserve breached"
        raise CheckError(f"ROCm safety stop: {reason}; private evidence: {evidence}")


def _project_exists(project: str) -> bool:
    try:
        result = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
                                 "--format", "{{.ID}}"], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False, timeout=30,
                                env=safe_environment())
        volumes = subprocess.run(["docker", "volume", "ls", "--filter", f"label=com.docker.compose.project={project}",
                                  "--format", "{{.Name}}"], text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, check=False, timeout=30,
                                 env=safe_environment())
        networks = subprocess.run(["docker", "network", "ls", "--filter", f"label=com.docker.compose.project={project}",
                                   "--format", "{{.Name}}"], text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, check=False, timeout=30,
                                  env=safe_environment())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError("cannot verify Docker project ownership") from exc
    if result.returncode or volumes.returncode or networks.returncode:
        raise CheckError("cannot verify Docker project ownership")
    return bool(result.stdout.strip() or volumes.stdout.strip() or networks.stdout.strip())


def project_owns_api_port(project: str) -> bool:
    result = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
                             "--format", "{{.Ports}}"], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=30, env=safe_environment())
    if result.returncode:
        raise CheckError("cannot verify API port ownership")
    return any(re.search(r"(?:^|, )(?:(?:0\.0\.0\.0|127\.0\.0\.1|\[::\]|::):)?8080->", line)
               for line in result.stdout.splitlines())


def existing_install(args: argparse.Namespace, candidate: Path) -> None:
    install_dir = args.install_dir
    env_file = install_dir / "deploy/compose/.env"
    compose_file = install_dir / "deploy/compose/compose.yml"
    if not install_dir.exists() or (install_dir.is_dir() and not any(install_dir.iterdir())):
        fresh_keep_install(args, candidate)
        return
    if not install_dir.is_dir() or not env_file.is_file() or not compose_file.is_file():
        raise CheckError("--keep requires a real existing .env/compose install pair or an empty directory")
    before = api_base(install_dir)
    values = compose_environment(env_file, compose_file)
    project = values.get("COMPOSE_PROJECT_NAME", "").strip() or "potocolom"
    if not project_owns_api_port(project):
        raise CheckError("the configured install does not own API port 8080; refusing to attach to another service")
    wait_ready(before)
    saved = snapshot_assets(before)
    env_before = values
    logs = Path(tempfile.mkdtemp(prefix="release-check-logs-"))
    passed = False
    try:
        run_installer(candidate, args.version, install_dir, args.release_dir, args.profile, logs)
        env_after = parse_env(env_file)
        assert_env_preserved(env_before, env_after)
        if not project_owns_api_port(project):
            raise CheckError("upgraded install no longer owns API port 8080")
        after = api_base(install_dir)
        wait_ready(after)
        verify_assets(after, saved)
        safety_check = (lambda: rocm_safety_check(install_dir, project, logs)) if args.profile == "rocm" else None
        generate(after, "release install check preserved existing library", safety_check)
        passed = True
    finally:
        if passed:
            shutil.rmtree(logs, ignore_errors=True)
        else:
            print(f"private installer logs retained at {logs}", file=sys.stderr)


def fresh_keep_install(args: argparse.Namespace, candidate: Path) -> None:
    install_dir = args.install_dir
    if install_dir.exists() and (not install_dir.is_dir() or any(install_dir.iterdir())):
        raise CheckError("--keep install directory must be absent or empty")
    free_port_8080()
    stamp = secrets.token_hex(8)
    project = f"release-check-{stamp}"
    logs = Path(tempfile.mkdtemp(prefix="release-check-logs-"))
    passed = False
    try:
        prepare_fresh(install_dir, project, args.release_dir, args.version)
        if _project_exists(project):
            raise CheckError(f"owned test project already exists: {project}")
        run_installer(candidate, args.version, install_dir, args.release_dir, args.profile, logs)
        if not project_owns_api_port(project):
            raise CheckError("fresh install does not own API port 8080")
        base = api_base(install_dir)
        wait_ready(base)
        safety_check = (lambda: rocm_safety_check(install_dir, project, logs)) if args.profile == "rocm" else None
        generate(base, "release install check first generation", safety_check)
        passed = True
    finally:
        if passed:
            shutil.rmtree(logs, ignore_errors=True)
        else:
            print(f"private installer logs retained at {logs}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", required=True)
    result.add_argument("--release-dir", type=Path, required=True)
    result.add_argument("--install-dir", type=Path, required=True)
    result.add_argument("--previous-release-dir", type=Path)
    result.add_argument("--previous-version")
    result.add_argument("--profile", choices=("gpu", "rocm"), required=True)
    result.add_argument("--keep", action="store_true", help="preserve and upgrade the supplied existing install")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        candidate, _ = verify_release(args.release_dir, args.version)
        if args.keep:
            existing_install(args, candidate)
        else:
            if not args.previous_release_dir or not args.previous_version:
                raise CheckError("fresh rehearsal requires --previous-release-dir and --previous-version")
            if args.install_dir.exists() and any(args.install_dir.iterdir()):
                raise CheckError("fresh install directory must be empty")
            fresh_rehearsal(args, candidate)
        print(f"PASS release check {args.version} profile={args.profile}")
        return 0
    except CheckError as exc:
        print(f"FAIL release check: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
