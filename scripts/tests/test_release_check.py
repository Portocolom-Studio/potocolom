from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import tarfile
import threading
import time
import unittest
import zlib
from unittest.mock import Mock, patch
from contextlib import redirect_stderr

DRIVER = Path(__file__).resolve().parents[1] / "release-check.py"
SPEC = importlib.util.spec_from_file_location("release_check", DRIVER)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)


def png(width: int = 768, height: int = 768, *, raw: bytes | None = None,
        color: int = 2, depth: int = 8, interlace: int = 0,
        compressed: bytes | None = None) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
    pixels = b"\x00" + b"\x00" * (width * (4 if color == 6 else 3))
    if compressed is None:
        compressed = zlib.compress(raw if raw is not None else pixels * height)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


def release_fixture(directory: Path, version: str = "v1.2.3") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    bundle = directory / f"potocolom-{version}-selfhost.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        data = b"AUTH_MODE=none\n"
        info = tarfile.TarInfo("deploy/compose/.env.example")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    (directory / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    installer_hash = hashlib.sha256((directory / "install.sh").read_bytes()).hexdigest()
    bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (directory / "SHA256SUMS").write_text(
        f"{installer_hash}  install.sh\n{bundle_hash}  {bundle.name}\n", encoding="utf-8")
    return directory


class ReleaseCheckTests(unittest.TestCase):

    def test_generation_request_matches_the_release_model_schema(self):
        manifest_path = Path(check.__file__).resolve().parents[1] / "worker/models" / f"{check.MODEL}.json"
        manifest = json.loads(manifest_path.read_text())
        requests = []
        def api(_base, path, **kwargs):
            if path == "/api/v1/models":
                return [{"id": check.MODEL}]
            if path == "/api/v1/generations":
                requests.append(kwargs["body"])
                params = kwargs["body"]["params"]
                schema = manifest["parameters"]["properties"]
                for dimension in ("width", "height"):
                    self.assertIn(params[dimension], schema[dimension]["enum"])
                self.assertGreaterEqual(params["steps"], schema["steps"]["minimum"])
                self.assertLessEqual(params["steps"], schema["steps"]["maximum"])
                return {"job_id": "made"}
            return {"state": "succeeded", "assets": [{"id": "asset", "url": "/asset"}]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers.get_content_type.return_value = "image/png"
        response.read.return_value = png()
        with patch.object(check, "http_json", side_effect=api), patch.object(check, "urlopen", return_value=response):
            check.generate("http://localhost:8080", "candidate or previous release")
        self.assertEqual(requests[0]["params"]["width"], 768)
        self.assertEqual(requests[0]["params"]["height"], 768)
        self.assertEqual(requests[0]["params"]["steps"], 8)

    def test_cleanup_refuses_unexpected_resource_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".release-check-project").write_text("release-check-0123456789abcdef")
            for kind, bad in (("ps", "not-an-id"), ("network", "not-an-id"), ("volume", "potocolom_assets")):
                def command(args, **_kwargs):
                    return bad if args[1] == kind else ""
                with self.subTest(kind=kind), patch.object(check, "run", side_effect=command) as docker:
                    with self.assertRaisesRegex(check.CheckError, "refusing cleanup"):
                        check.cleanup_rehearsal(root)
                self.assertFalse(any(c.args[0][1] == "rm" or c.args[0][2:3] == ["rm"] for c in docker.call_args_list))

    def test_upgrade_restores_default_models_and_candidate_image_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = release_fixture(root / "candidate")
            previous = release_fixture(root / "previous", "v1.2.2")
            args = argparse.Namespace(install_dir=root / "install", version="v1.2.3",
                                      release_dir=release, previous_release_dir=previous,
                                      previous_version="v1.2.2", profile="gpu")
            events = []
            def installer(_file, version, *_args, **_kwargs):
                events.append(("install", version))
            def generated(_base, _prompt, _guard, *, size=768):
                events.append(("generate", size))
                return "saved", png(size, size)
            def model_config(*_args, restore=False):
                events.append(("models", "restore" if restore else "select"))
            with patch.object(check, "free_port_8080"), \
                    patch.object(check, "_project_exists", side_effect=[False, True]), \
                    patch.object(check, "run_installer", side_effect=installer), \
                    patch.object(check, "legacy_model_config", side_effect=model_config), \
                    patch.object(check, "compose_owned"), patch.object(check, "remove_owned_data_volumes"), \
                    patch.object(check, "cleanup_rehearsal"), \
                    patch.object(check, "project_owns_api_port", return_value=True), \
                    patch.object(check, "api_base", return_value="http://localhost:8080"), \
                    patch.object(check, "wait_ready"), patch.object(check, "parse_env", return_value={}), \
                    patch.object(check, "generate", side_effect=generated), \
                    patch.object(check, "verify_assets") as saved:
                check.fresh_rehearsal(args, release / "install.sh")
            self.assertEqual(events, [("install", "v1.2.3"), ("generate", 768),
                                     ("models", "select"), ("install", "v1.2.2"),
                                     ("generate", 768), ("models", "restore"),
                                     ("install", "v1.2.3"), ("generate", 768)])
            saved.assert_called_once_with("http://localhost:8080", {"saved": hashlib.sha256(png()).hexdigest()})

    def test_cleanup_refuses_user_projects_before_docker_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for project in ("potocolom", "release-check-short", "release-check-0123456789abcdef\nother"):
                (root / ".release-check-project").write_text(project)
                with self.subTest(project=project), patch.object(check, "run", return_value="") as command:
                    with self.assertRaisesRegex(check.CheckError, "refusing cleanup"):
                        check.cleanup_rehearsal(root)
                    command.assert_not_called()

    def test_worker_disappearance_is_safe_during_each_read_only_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            for stage in ("inspect", "logs", "exec"):
                alive = [True]

                def command(args, **_kwargs):
                    if args[1] == stage:
                        alive[0] = False
                    if not alive[0]:
                        raise check.CommandFailed("docker", 1, "No such container", None)
                    return "VRAM_RESERVE_MB=1536\n" if args[1] == "inspect" else ""

                with self.subTest(stage=stage), patch.object(check, "run", side_effect=command), \
                        patch.object(check, "owned_rocm_worker_ids", side_effect=lambda _: ["0123456789ab"] if alive[0] else []), \
                        patch.object(check, "rocm_vram_usage", return_value=[("card0", 16 * 1024**3, 2 * 1024**3)]):
                    check.rocm_safety_check("owned", Path(tmp))
                self.assertFalse(alive[0])

    def test_live_reserve_values_have_finite_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value, valid in (("", True), ("VRAM_RESERVE_MB=256", True),
                                 ("VRAM_RESERVE_MB=65536", True), ("VRAM_RESERVE_MB=255", False),
                                 ("VRAM_RESERVE_MB=65537", False), ("VRAM_RESERVE_MB=bad", False),
                                 ("unexpected", False), ("OTHER=1536", False)):
                def command(args, **_kwargs):
                    return value if args[1] == "inspect" else ""

                with self.subTest(value=value), patch.object(check, "run", side_effect=command), \
                        patch.object(check, "owned_rocm_worker_ids", return_value=["0123456789ab"]), \
                        patch.object(check, "rocm_vram_usage", return_value=[("card0", 128 * 1024**3, 1024**3)]):
                    if valid:
                        check.rocm_safety_check("owned", Path(tmp))
                    else:
                        with self.assertRaisesRegex(check.CheckError, "VRAM_RESERVE_MB"):
                            check.rocm_safety_check("owned", Path(tmp))

    def test_disk_probe_retries_a_worker_restart_and_bounds_persistent_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.write_text("VRAM_RESERVE_MB=1536\n")
            env.with_name("compose.yml").write_text("services: {}\n")
            for persistent in (False, True):
                probes = []

                def command(args, **_kwargs):
                    if args[:2] == ["docker", "exec"]:
                        probes.append(True)
                        if len(probes) == 1 or persistent:
                            raise check.CommandFailed("docker", 128, "worker restarted during probe", None)
                    return "VRAM_RESERVE_MB=1536\n" if "inspect" in args else ""

                with patch.object(check, "run", side_effect=command), \
                        patch.object(check, "owned_rocm_worker_ids", return_value=["0123456789ab"]), \
                        patch.object(check, "rocm_vram_usage", return_value=[("card0", 16 * 1024**3, 2 * 1024**3)]):
                    if persistent:
                        with self.assertRaisesRegex(check.CheckError, "exited 128"):
                            check.rocm_safety_check("owned", root / "logs")
                    else:
                        check.rocm_safety_check("owned", root / "logs")
                self.assertEqual(len(probes), 2)

    def test_stopped_process_wins_over_stale_docker_running_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.write_text("VRAM_RESERVE_MB=1536\n")
            env.with_name("compose.yml").write_text("services: {}\n")
            message = "OCI runtime exec failed: exec failed: cannot exec in a stopped container"
            for code, output in ((128, message), (1, message), (128, "permission denied")):
                probes = []

                def command(args, **_kwargs):
                    if args[:2] == ["docker", "exec"]:
                        probes.append(True)
                        raise check.CommandFailed("docker", code, output, None)
                    return "VRAM_RESERVE_MB=1536\n" if "inspect" in args else ""

                with patch.object(check, "run", side_effect=command), \
                        patch.object(check, "owned_rocm_worker_ids", return_value=["0123456789ab"]), \
                        patch.object(check, "rocm_vram_usage", return_value=[("card0", 16 * 1024**3, 2 * 1024**3)]):
                    if code == 128 and output == message:
                        check.rocm_safety_check("owned", root / "logs")
                        self.assertEqual(len(probes), 1)
                    else:
                        with self.assertRaises(check.CommandFailed):
                            check.rocm_safety_check("owned", root / "logs")
                        self.assertEqual(len(probes), 2)

    def test_png_requires_all_pixels_and_valid_filters(self):
        complete = bytes((1 + 768 * 3) * 768)
        for raw in (b"\x00", complete[:-1], complete + b"\x00", b"\x05" + complete[1:]):
            with self.subTest(length=len(raw)), self.assertRaisesRegex(check.CheckError, "scanlines"):
                check.validate_png(png(raw=raw))
        for encoding in ({"color": 0}, {"depth": 16}, {"interlace": 1}):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(check.CheckError, "8-bit RGB"):
                check.validate_png(png(**encoding))
        compressed = zlib.compress(complete)
        for payload in (compressed[:-1], compressed + b"trailing"):
            with self.subTest(payload=payload[-8:]), self.assertRaises(check.CheckError):
                check.validate_png(png(compressed=payload))
        self.assertEqual(check.validate_png(png(color=6)), (768, 768))

    def test_guard_interrupts_an_installer_and_joins_its_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            started = root / "started"
            late = root / "late"

            def unsafe(*_args):
                if started.exists():
                    raise check.CheckError("ROCm safety stop: warmup")

            child = "import pathlib,time; pathlib.Path(%r).touch(); time.sleep(1); pathlib.Path(%r).touch()" % (str(started), str(late))
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(20)"
            with patch.object(check, "rocm_safety_check", side_effect=unsafe), \
                    patch.object(check, "stop_owned_rocm_workers") as stop, \
                    self.assertRaisesRegex(check.CheckError, "ROCm safety stop"):
                with check.RocmGuard("rocm", "owned-project", root / "logs") as guard:
                    guard.start()
                    check.run([sys.executable, "-c", parent, child], timeout=10, abort=guard.failed)
            self.assertFalse(guard.thread.is_alive())
            stop.assert_called_once_with("owned-project", root / "logs")
            time.sleep(1.1)
            self.assertFalse(late.exists())

    def test_guard_covers_the_initial_model_wait(self):
        waiting = threading.Event()
        requests = []

        def unsafe(*_args):
            if waiting.is_set():
                raise check.CheckError("ROCm safety stop: no hello")

        def models(*_args, **_kwargs):
            requests.append(True)
            if waiting.is_set() and guard.failed.is_set():
                raise RuntimeError("unsafe model wait was not interrupted")
            waiting.set()
            return []

        with patch.object(check, "rocm_safety_check", side_effect=unsafe), \
                patch.object(check, "stop_owned_rocm_workers"), \
                patch.object(check, "http_json", side_effect=models), \
                self.assertRaisesRegex(check.CheckError, "ROCm safety stop"):
            with check.RocmGuard("rocm", "owned", Path("unused")) as guard:
                guard.start()
                with patch.object(check.time, "sleep", side_effect=lambda _: guard.failed.wait(2)):
                    check.generate("http://localhost:8080", "test", guard.check)
        self.assertEqual(len(requests), 1)

    def test_guard_joins_a_check_in_progress_before_returning(self):
        entered, release, finished = (threading.Event() for _ in range(3))

        def checking(*_args):
            entered.set()
            release.wait(2)
            finished.set()

        guard = check.RocmGuard("rocm", "owned", Path("unused"))
        with patch.object(check, "rocm_safety_check", side_effect=checking):
            try:
                with guard:
                    guard.start()
                    self.assertTrue(entered.wait(2))
                    timer = threading.Timer(0.1, release.set)
                    timer.start()
                self.assertTrue(finished.is_set())
                self.assertFalse(guard.thread.is_alive())
            finally:
                release.set()
                guard.thread.join()
                timer.join()

    def test_keep_guard_stops_unsafe_work_during_readiness_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "install"
            env = install / "deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.write_text("AUTH_MODE=none\n")
            env.with_name("compose.yml").write_text("services: {}\n")
            ready, stopped = threading.Event(), threading.Event()

            def unsafe(*_args):
                if ready.is_set():
                    raise check.CheckError("ROCm safety stop: warmup")

            def readiness(*_args):
                ready.set()
                self.assertTrue(stopped.wait(2))
                raise check.CheckError("store unavailable")

            args = argparse.Namespace(install_dir=install, profile="rocm")
            with patch.object(check, "api_base", return_value="http://localhost:8080"), \
                    patch.object(check, "compose_environment", return_value={"COMPOSE_PROJECT_NAME": "owned"}), \
                    patch.object(check, "project_owns_api_port", return_value=True), \
                    patch.object(check, "rocm_safety_check", side_effect=unsafe), \
                    patch.object(check, "stop_owned_rocm_workers", side_effect=lambda *_: stopped.set()), \
                    patch.object(check, "wait_ready", side_effect=readiness), \
                    patch.object(check, "run_installer") as installer, \
                    patch.object(check.shutil, "rmtree") as remove, \
                    redirect_stderr(io.StringIO()), self.assertRaisesRegex(check.CheckError, "ROCm safety stop"):
                check.existing_install(args, root / "candidate.sh")
            self.assertTrue(env.is_file())
            installer.assert_not_called()
            remove.assert_not_called()

    def test_both_fresh_modes_start_the_guard_before_the_installer(self):
        for keep in (False, True):
            with self.subTest(keep=keep), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                release = release_fixture(root / "release")
                previous = release_fixture(root / "previous", "v1.2.2")
                args = argparse.Namespace(install_dir=root / "install", release_dir=release,
                                          version="v1.2.3", profile="rocm", keep=keep,
                                          previous_release_dir=previous, previous_version="v1.2.2")

                def installer(*_args, abort=None):
                    self.assertTrue(abort.wait(2), "guard did not cover installer startup")
                    raise check.CheckError("installer interrupted")

                with patch.object(check, "free_port_8080"), \
                        patch.object(check, "_project_exists", return_value=False), \
                        patch.object(check, "rocm_safety_check", side_effect=check.CheckError("ROCm safety stop")), \
                        patch.object(check, "stop_owned_rocm_workers"), \
                        patch.object(check, "run_installer", side_effect=installer), \
                        redirect_stderr(io.StringIO()), self.assertRaisesRegex(check.CheckError, "ROCm safety stop"):
                    (check.fresh_keep_install if keep else check.fresh_rehearsal)(args, release / "install.sh")
                self.assertEqual(args.install_dir.exists(), keep)

    def test_release_checksum_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = release_fixture(Path(tmp) / "release")
            installer, _ = check.verify_release(path, "v1.2.3")
            self.assertEqual(installer.name, "install.sh")
            (path / "potocolom-v1.2.3-selfhost.tar.gz").write_bytes(b"changed")
            with self.assertRaisesRegex(check.CheckError, "does not verify"):
                check.verify_release(path, "v1.2.3")

    def test_invalid_version_fails_release_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(check.CheckError, "version must"):
                check.verify_release(Path(tmp), "-v1.2.3")

    def test_cli_checksum_failure_precedes_installer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = release_fixture(Path(tmp) / "release")
            (release / "potocolom-v1.2.3-selfhost.tar.gz").write_bytes(b"corrupt")
            arguments = ["release-check.py", "--version", "v1.2.3", "--release-dir", str(release),
                         "--install-dir", str(Path(tmp) / "install"), "--profile", "gpu",
                         "--previous-release-dir", str(release), "--previous-version", "v1.2.2"]
            with patch.object(sys, "argv", arguments), patch.object(check, "run_installer") as installer, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(check.main(), 1)
            installer.assert_not_called()

    def test_cli_requires_core_arguments(self) -> None:
        with patch.object(sys, "argv", ["release-check.py"]), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            check.parser().parse_args()

    def test_installer_receives_file_url_and_private_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = release_fixture(root / "release")
            install_dir = root / "install space"
            with patch.dict(os.environ, {"GH_TOKEN": "secret-token", "AWS_SECRET_ACCESS_KEY": "secret-key"}), \
                    patch.object(check, "parse_env", return_value={"POTOCOLOM_VERSION": "v1.2.3"}), \
                    patch.object(check, "run") as run:
                check.run_installer(release / "install.sh", "v1.2.3", install_dir,
                                    release, "rocm", root / "logs")
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["bash", str(release / "install.sh")])
            self.assertEqual(kwargs["env"]["POTOCOLOM_RELEASE_URL"], release.resolve().as_uri())
            self.assertEqual(kwargs["env"]["POTOCOLOM_DIR"], str(install_dir))
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", kwargs["env"])
            self.assertIn("installer-v1.2.3-", str(kwargs["log"]))

    def test_installer_must_pin_the_requested_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(check, "run"), \
                    patch.object(check, "parse_env", return_value={"POTOCOLOM_VERSION": "v1.2.2"}), \
                    self.assertRaisesRegex(check.CheckError, "did not pin"):
                check.run_installer(root / "install.sh", "v1.2.3", root / "install",
                                    root, "rocm", root / "logs")

    def test_png_validation_checks_signature_type_and_dimensions(self) -> None:
        body = png()
        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        fake.read = Mock(return_value=body)
        fake.headers = Mock()
        fake.headers.get_content_type.return_value = "image/png"
        with patch.object(check, "urlopen", return_value=fake):
            self.assertEqual(check.local_asset("http://localhost:8080", {"url": "/api/v1/assets/a"}), body)
        fake.headers.get_content_type.return_value = "text/html"
        with patch.object(check, "urlopen", return_value=fake), self.assertRaisesRegex(
                check.CheckError, "not a non-empty PNG"):
            check.local_asset("http://localhost:8080", {"url": "/api/v1/assets/a"})
        fake.headers.get_content_type.return_value = "image/png"
        fake.read.return_value = png(512, 512)
        with patch.object(check, "urlopen", return_value=fake), self.assertRaisesRegex(
                check.CheckError, "unexpected dimensions"):
            check.local_asset("http://localhost:8080", {"url": "/api/v1/assets/a"})

    def test_remote_asset_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(check.CheckError, "non-local"):
            check.local_asset("http://localhost:8080", {"url": "https://elsewhere.test/image.png"})

    def test_generation_failure_stops_immediately(self) -> None:
        def api(_base: str, path: str, **kwargs: object) -> object:
            if path == "/api/v1/models":
                return [{"id": check.MODEL}]
            if path == "/api/v1/generations":
                return {"job_id": "job-1"}
            return {"state": "failed", "failure_reason": "worker error"}

        with patch.object(check, "http_json", side_effect=api), self.assertRaisesRegex(
                check.CheckError, "generation failed"):
            check.generate("http://localhost:8080", "prompt")

    def test_generation_timeout_is_finite(self) -> None:
        ticks = iter([0.0, 0.0, 1900.0])

        def api(_base: str, path: str, **kwargs: object) -> object:
            if path == "/api/v1/models":
                return [{"id": check.MODEL}]
            if path == "/api/v1/generations":
                return {"job_id": "job-1"}
            return {"state": "running"}

        with patch.object(check, "http_json", side_effect=api), \
                patch.object(check.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(check.time, "sleep"), self.assertRaisesRegex(check.CheckError, "timed out"):
            check.generate("http://localhost:8080", "prompt")

    def test_asset_snapshot_detects_missing_and_changed_assets(self) -> None:
        pages = iter([
            [{"id": "job-1", "assets": [{"id": "asset-1", "url": "/asset"}]}],
            [],
        ])
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [png(), b""]
        response.headers = Mock()
        response.headers.get_content_type.return_value = "image/png"
        with patch.object(check, "http_json", side_effect=lambda *a, **k: next(pages)), \
                patch.object(check, "urlopen", return_value=response):
            self.assertEqual(check.snapshot_assets("http://localhost:8080"),
                             {"asset-1": hashlib.sha256(png()).hexdigest()})
        with patch.object(check, "snapshot_assets", return_value={}):
            with self.assertRaisesRegex(check.CheckError, "missing or changed"):
                check.verify_assets("http://localhost:8080", {"asset-1": "hash"})

    def test_saved_assets_are_hashed_as_raw_bytes_without_generation_rules(self) -> None:
        bodies = [b"jpeg bytes", b"webp bytes", png(1024, 1024), b"thumbnail"]
        pages = iter([[{"id": f"job-{i}", "assets": [{"id": f"asset-{i}", "url": f"/{i}"}]}
                      for i in range(4)], []])
        responses = []
        for body, kind in zip(bodies, ("application/octet-stream", "image/webp", "image/png", "image/png")):
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.side_effect = [body, b""]
            response.headers = Mock()
            response.headers.get_content_type.return_value = kind
            responses.append(response)
        with patch.object(check, "http_json", side_effect=lambda *a, **k: next(pages)), \
                patch.object(check, "urlopen", side_effect=responses):
            self.assertEqual(check.snapshot_assets("http://localhost:8080"),
                             {f"asset-{i}": hashlib.sha256(body).hexdigest()
                              for i, body in enumerate(bodies)})

    def test_ready_wait_retries_network_and_503_and_requires_empty_200(self) -> None:
        events = []

        def open_ready(request, timeout):
            events.append(request.full_url)
            state = len(events)
            if state == 1:
                raise ConnectionResetError("API is starting")
            if state == 2:
                raise check.URLError("connection refused")
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.status = 503 if state == 3 else 200
            response.read.return_value = b"" if state == 5 else b"not ready"
            return response

        ticks = iter([0, 0, 0, 0, 0, 0, 10])
        with patch.object(check, "urlopen", side_effect=open_ready), \
                patch.object(check.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(check.time, "sleep"):
            check.wait_ready("https://studio.example")
        self.assertEqual(len(events), 5)
        self.assertTrue(all(url.endswith("/api/v1/ready") for url in events))

    def test_compose_environment_is_read_from_explicit_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            env.write_text("export PUBLIC_URL = https://studio.example\nAUTH_MODE: none\n", encoding="utf-8")
            compose = root / "compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            output = "PUBLIC_URL=https://studio.example\nAUTH_MODE=none\nDOCKER_CLI_PLUGIN_SOCKET=/tmp/one-call.sock\n"
            with patch.object(check, "run", return_value=output) as command:
                values = check.compose_environment(env, compose)
            self.assertEqual(values["PUBLIC_URL"], "https://studio.example")
            self.assertEqual(values["AUTH_MODE"], "none")
            self.assertNotIn("DOCKER_CLI_PLUGIN_SOCKET", values)
            self.assertIn("--env-file", command.call_args.args[0])
            self.assertIn("config", command.call_args.args[0])

    def test_compose_reads_real_export_colon_and_spacing_rules_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            env.write_text('export PUBLIC_URL = "https://studio.example" # proxy\nAUTH_MODE : none\n',
                           encoding="utf-8")
            compose = root / "compose.yml"
            compose.write_text('services:\n  fixture:\n    image: alpine\n', encoding="utf-8")
            values = check.compose_environment(env, compose)
            self.assertEqual(values["PUBLIC_URL"], "https://studio.example")
            self.assertEqual(values["AUTH_MODE"], "none")

    def test_public_url_accepts_lan_and_https_origins_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp)
            compose_dir = install / "deploy/compose"
            compose_dir.mkdir(parents=True)
            (compose_dir / ".env").write_text("PUBLIC_URL=https://studio.example\n", encoding="utf-8")
            (compose_dir / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            for origin in ("http://192.168.1.8:8080", "https://studio.example"):
                with patch.object(check, "compose_environment", return_value={"PUBLIC_URL": origin}):
                    self.assertEqual(check.api_base(install), origin)
            with patch.object(check, "compose_environment", return_value={"PUBLIC_URL": "https://user:pw@studio.example"}):
                with self.assertRaisesRegex(check.CheckError, "HTTP\(S\) origin"):
                    check.api_base(install)

    def test_keep_can_install_into_empty_directory_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "empty install"
            release = release_fixture(Path(tmp) / "release")
            args = argparse.Namespace(install_dir=install, version="v1.2.3", release_dir=release,
                                      profile="rocm", keep=True)
            candidate = release / "install.sh"
            with patch.object(check, "free_port_8080"), \
                    patch.object(check, "_project_exists", return_value=False), \
                    patch.object(check, "project_owns_api_port", return_value=True), \
                    patch.object(check, "api_base", return_value="http://localhost:8080"), \
                    patch.object(check, "compose_owned") as compose, \
                    patch.object(check.shutil, "rmtree") as remove, \
                    patch.object(check, "run_installer") as installer, \
                    patch.object(check, "wait_ready") as ready, \
                    patch.object(check, "generate", return_value=("a", png())), \
                    patch.object(check, "compose_environment", return_value={"AUTH_MODE": "none"}):
                check.existing_install(args, candidate)
            installer.assert_called_once()
            ready.assert_called_once()
            compose.assert_not_called()
            self.assertTrue(all(call.args[0] != install for call in remove.call_args_list))
            self.assertTrue(install.is_dir())

    def test_keep_first_install_failure_preserves_directory_and_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = release_fixture(root / "release")
            install = root / "external fixture" / "empty"
            args = argparse.Namespace(install_dir=install, version="v1.2.3", release_dir=release,
                                      profile="gpu", keep=True)
            with patch.object(check, "free_port_8080"), \
                    patch.object(check, "_project_exists", return_value=False), \
                    patch.object(check, "run_installer", side_effect=check.CheckError("installer failed")), \
                    patch.object(check, "compose_owned") as compose, \
                    patch.object(check.shutil, "rmtree") as remove, \
                    patch.object(check.tempfile, "mkdtemp", return_value=str(root / "private logs")), \
                    redirect_stderr(io.StringIO()), self.assertRaisesRegex(check.CheckError, "installer failed"):
                check.existing_install(args, release / "install.sh")
            compose.assert_not_called()
            remove.assert_not_called()
            self.assertTrue((install / "deploy/compose/.env").is_file())

    def test_keep_refuses_nonempty_non_install_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "unrelated"
            install.mkdir()
            (install / "notes.txt").write_text("keep", encoding="utf-8")
            args = argparse.Namespace(install_dir=install, version="v1.2.3", release_dir=Path(tmp),
                                      profile="gpu", keep=True)
            with patch.object(check, "run_installer") as installer, \
                    self.assertRaisesRegex(check.CheckError, "real existing .env/compose"):
                check.existing_install(args, Path(tmp) / "install.sh")
            installer.assert_not_called()
            self.assertEqual((install / "notes.txt").read_text(encoding="utf-8"), "keep")

    def test_private_command_timeout_joins_the_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "late-child"
            child = "import time,pathlib; time.sleep(1); pathlib.Path(%r).write_text('late')" % str(marker)
            command = [sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(30)", child]
            with self.assertRaisesRegex(check.CheckError, "timed out"):
                check.run(command, timeout=0.1, log=Path(tmp) / "private.log")
            time.sleep(1.2)
            self.assertFalse(marker.exists(), "timed-out descendants must not survive cleanup")

    def test_rocm_guard_ignores_small_igpu_and_stops_only_owned_worker_on_breach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "install/deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.with_name("compose.yml").write_text("services: {}\n", encoding="utf-8")
            env.write_text("VRAM_RESERVE_MB=1536\n", encoding="utf-8")
            sysfs = root / "sys"
            for name, total in (("card0", 512 * 1024 * 1024), ("card1", 8 * 1024 * 1024 * 1024)):
                device = sysfs / name / "device"
                device.mkdir(parents=True)
                (device / "vendor").write_text("0x1002\n", encoding="ascii")
                (device / "mem_info_vram_total").write_text(str(total), encoding="ascii")
                used = 7 * 1024 * 1024 * 1024 if name == "card1" else total // 2
                (device / "mem_info_vram_used").write_text(str(used), encoding="ascii")
            docker = Mock(return_value=Mock(returncode=0, stdout="0123456789ab\n"))
            logs = root / "private"
            with patch.object(check.subprocess, "run", docker), \
                    patch.object(check, "run", return_value=""), \
                    patch.object(check, "stop_owned_rocm_workers") as stop, \
                    self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                check.rocm_safety_check("owned-project", logs, sysfs)
            stop.assert_called_once_with("owned-project", logs)
            self.assertIn("card1", (logs / "rocm-safety-evidence.log").read_text(encoding="utf-8"))
            self.assertIn("--filter", docker.call_args.args[0])

    def test_rocm_group_offload_stops_verified_worker_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "install/deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.with_name("compose.yml").write_text("services: {}\n", encoding="utf-8")
            env.write_text("VRAM_RESERVE_MB=1536\n", encoding="utf-8")
            device = root / "sys/card0/device"
            device.mkdir(parents=True)
            (device / "vendor").write_text("0x1002", encoding="ascii")
            (device / "mem_info_vram_total").write_text(str(8 * 1024**3), encoding="ascii")
            (device / "mem_info_vram_used").write_text(str(1 * 1024**3), encoding="ascii")
            response = Mock(returncode=0, stdout="0123456789ab\n")
            with patch.object(check.subprocess, "run", return_value=response), \
                    patch.object(check, "run", side_effect=["", "demoting from model_offload to group_offload", ""]), \
                    patch.object(check, "stop_owned_rocm_workers") as stop, \
                    self.assertRaisesRegex(check.CheckError, "group offload rung"):
                check.rocm_safety_check("owned-project", root / "private", device.parent.parent)
            stop.assert_called_once_with("owned-project", root / "private")

    def test_initial_disk_offload_is_stopped_without_a_demotion_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "install/deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.with_name("compose.yml").write_text("services: {}\n", encoding="utf-8")
            env.write_text("VRAM_RESERVE_MB=1536\n", encoding="utf-8")
            response = Mock(returncode=0, stdout="0123456789ab\n")
            with patch.object(check, "rocm_vram_usage", return_value=[("card0", 16 * 1024**3, 2 * 1024**3)]), \
                    patch.object(check.subprocess, "run", return_value=response), \
                    patch.object(check, "run", side_effect=["", "", "/models/.offload/ssd-1b-lightning\n"]) as run, \
                    patch.object(check, "stop_owned_rocm_workers") as stop, \
                    self.assertRaisesRegex(check.CheckError, "group offload rung"):
                check.rocm_safety_check("owned-project", root / "private")
            self.assertEqual(run.call_args.args[0][:3], ["docker", "exec", "0123456789ab"])
            stop.assert_called_once_with("owned-project", root / "private")

    def test_rocm_guard_reads_the_live_worker_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "install/deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.with_name("compose.yml").write_text("services: {}\n", encoding="utf-8")
            env.write_text("VRAM_RESERVE_MB=3072\n", encoding="utf-8")
            with patch.object(check, "rocm_vram_usage", return_value=[("card0", 16 * 1024**3, 14 * 1024**3)]), \
                    patch.object(check, "owned_rocm_worker_ids", return_value=["0123456789ab"]), \
                    patch.object(check, "run", side_effect=["VRAM_RESERVE_MB=3072\n", "", ""]), \
                    patch.dict(os.environ, {"VRAM_RESERVE_MB": "1536"}), \
                    patch.object(check, "stop_owned_rocm_workers") as stop, \
                    self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                check.rocm_safety_check("owned-project", root / "private")
            stop.assert_called_once_with("owned-project", root / "private")

    def test_rocm_guard_does_not_charge_the_small_igpu_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "install/deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.with_name("compose.yml").write_text("services: {}\n", encoding="utf-8")
            env.write_text("VRAM_RESERVE_MB=1536\n", encoding="utf-8")
            for name, total, used in (("card0", 512 * 1024**2, 256 * 1024**2),
                                      ("card1", 16 * 1024**3, 2 * 1024**3)):
                device = root / "sys" / name / "device"
                device.mkdir(parents=True)
                for field, value in (("vendor", "0x1002"), ("mem_info_vram_total", str(total)),
                                     ("mem_info_vram_used", str(used))):
                    (device / field).write_text(value, encoding="ascii")
            with patch.object(check, "owned_rocm_worker_ids", return_value=["0123456789ab"]), \
                    patch.object(check, "run", return_value=""), \
                    patch.object(check, "stop_owned_rocm_workers") as stop:
                check.rocm_safety_check("owned-project", root / "private", root / "sys")
            stop.assert_not_called()
        with patch.object(check, "snapshot_assets", return_value={"asset-1": "changed"}):
            with self.assertRaisesRegex(check.CheckError, "missing or changed"):
                check.verify_assets("http://localhost:8080", {"asset-1": "hash"})

    def test_accounts_mode_refuses_before_installer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp)
            env = install / "deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.write_text("AUTH_MODE=accounts\n", encoding="utf-8")
            (env.parent / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            args = argparse.Namespace(install_dir=install, version="v1.2.3", release_dir=install,
                                      profile="gpu", keep=True)
            candidate = release_fixture(Path(tmp) / "release") / "install.sh"
            with patch.object(check, "compose_environment", return_value={"AUTH_MODE": "accounts"}), \
                    patch.object(check, "run_installer") as installer, self.assertRaisesRegex(
                    check.CheckError, "refusing before install changes"):
                check.existing_install(args, candidate)
            installer.assert_not_called()
            self.assertEqual(env.read_text(encoding="utf-8"), "AUTH_MODE=accounts\n")

    def test_env_preservation_allows_only_release_pin_change(self) -> None:
        check.assert_env_preserved({"POTOCOLOM_VERSION": "v1.2.2",
                                    "FLEET_SECRET": "private", "PUBLIC_URL": "http://localhost:8080"},
                                   {"POTOCOLOM_VERSION": "v1.2.3",
                                    "FLEET_SECRET": "private", "PUBLIC_URL": "http://localhost:8080"})
        with self.assertRaisesRegex(check.CheckError, "PUBLIC_URL"):
            check.assert_env_preserved({"PUBLIC_URL": "http://localhost:8080"},
                                       {"PUBLIC_URL": "http://elsewhere"})

    def test_keep_mode_never_cleans_supplied_install_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "install"
            env = install / "deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.write_text("AUTH_MODE=none\n", encoding="utf-8")
            (env.parent / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            args = argparse.Namespace(install_dir=install, version="v1.2.3", release_dir=install,
                                      profile="gpu", keep=True)
            candidate = release_fixture(Path(tmp) / "release") / "install.sh"
            with patch.object(check, "api_base", return_value="http://localhost:8080"), \
                    patch.object(check, "compose_environment", return_value={"AUTH_MODE": "none", "COMPOSE_PROJECT_NAME": "mine"}), \
                    patch.object(check, "project_owns_api_port", return_value=True), \
                    patch.object(check, "wait_ready"), patch.object(check, "snapshot_assets", return_value={}), \
                    patch.object(check, "run_installer", side_effect=check.CheckError("install failed")), \
                    patch.object(check.tempfile, "mkdtemp", return_value=str(Path(tmp) / "private-logs")), \
                    patch.object(check.shutil, "rmtree") as remove, redirect_stderr(io.StringIO()), \
                    self.assertRaises(check.CheckError):
                check.existing_install(args, candidate)
            remove.assert_not_called()
            self.assertTrue(env.exists())
            self.assertEqual(env.read_text(encoding="utf-8"), "AUTH_MODE=none\n")

    def test_readiness_precedes_every_post_upgrade_asset_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "install"
            env = install / "deploy/compose/.env"
            env.parent.mkdir(parents=True)
            env.write_text("AUTH_MODE=none\n", encoding="utf-8")
            (env.parent / "compose.yml").write_text("services: {}\n", encoding="utf-8")
            args = argparse.Namespace(install_dir=install, version="v1.2.3", release_dir=Path(tmp),
                                      profile="gpu", keep=True)
            candidate = release_fixture(Path(tmp) / "release") / "install.sh"
            events = []

            def installer(*_args, **_kwargs):
                events.append("installer")

            def ready(*_args):
                events.append("ready")

            def snapshot(*_args):
                events.append("snapshot")
                return {}

            with patch.object(check, "api_base", return_value="http://localhost:8080"), \
                    patch.object(check, "compose_environment", return_value={"AUTH_MODE": "none", "COMPOSE_PROJECT_NAME": "mine"}), \
                    patch.object(check, "project_owns_api_port", return_value=True), \
                    patch.object(check, "wait_ready", side_effect=ready), \
                    patch.object(check, "snapshot_assets", side_effect=snapshot), \
                    patch.object(check, "verify_assets", side_effect=lambda *_: events.append("verify")), \
                    patch.object(check, "generate", side_effect=lambda *_: ("new", png())), \
                    patch.object(check, "run_installer", side_effect=installer), \
                    patch.object(check.tempfile, "mkdtemp", return_value=str(Path(tmp) / "logs")):
                check.existing_install(args, candidate)
            self.assertLess(events.index("ready"), events.index("snapshot"))
            self.assertLess(events.index("installer"), events.index("verify"))
            post_upgrade = events.index("installer")
            self.assertLess(events.index("ready", post_upgrade), events.index("verify"))





    def test_fresh_failure_cleans_only_verified_owned_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = root / "fresh"
            previous = release_fixture(root / "previous", "v1.2.2")
            args = argparse.Namespace(install_dir=install, version="v1.2.3",
                                      release_dir=root, profile="rocm",
                                      previous_release_dir=previous, previous_version="v1.2.2")

            def prepare(directory: Path, _project: str, *_args) -> None:
                directory.mkdir(parents=True, exist_ok=True)

            with patch.object(check, "free_port_8080"), \
                    patch.object(check, "prepare_fresh", side_effect=prepare), \
                    patch.object(check, "_project_exists", side_effect=[False, True]), \
                    patch.object(check, "run_installer", side_effect=check.CheckError("candidate failed")), \
                    patch.object(check.tempfile, "mkdtemp", return_value=str(root / "private-logs")), \
                    patch.object(check, "cleanup_rehearsal") as cleanup, \
                    redirect_stderr(io.StringIO()), \
                    self.assertRaisesRegex(check.CheckError, "candidate failed"):
                check.fresh_rehearsal(args, root / "candidate/install.sh")
            cleanup.assert_called_once_with(install)
            self.assertFalse(install.exists())


if __name__ == "__main__":
    unittest.main()
