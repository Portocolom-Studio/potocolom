import secrets
import itertools
import os
import signal
import sys
import time
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.tests.test_release_check import check, release_fixture


class ReleaseGuardDockerTests(unittest.TestCase):
    def test_interrupt_joins_installer_children_and_removes_its_project(self):
        program = r'''import argparse, importlib.util, pathlib, sys
source, root, candidate, previous = map(pathlib.Path, sys.argv[1:5])
keep = sys.argv[5] == "keep"
spec = importlib.util.spec_from_file_location("release_check", source)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
check.free_port_8080 = lambda: None

def installer(_installer, _version, directory, *_args, **_kwargs):
    compose = directory / "deploy/compose/compose.yml"
    compose.write_text('services:\n  worker-rocm:\n    image: postgres:16\n    profiles: [rocm]\n    entrypoint: ["sh", "-c", "exec sleep 120"]\n')
    project = next(line.split("=", 1)[1] for line in compose.with_name(".env").read_text().splitlines() if line.startswith("COMPOSE_PROJECT_NAME="))
    (root / "project").write_text(project)
    check.compose_owned(directory, project, ["up", "-d", "--no-build", "worker-rocm"])
    child = "import pathlib,time;time.sleep(0.8);pathlib.Path(%r).touch()" % str(root / "late")
    parent = "import os,pathlib,subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1]]);pathlib.Path(sys.argv[2]).write_text(str(os.getpid()));time.sleep(30)"
    check.run([sys.executable, "-c", parent, child, str(root / "ready")], log=root / "installer.log")
check.run_installer = installer
sys.argv = [str(source), "--version", "v1.2.3", "--release-dir", str(candidate), "--previous-version", "v1.2.2", "--previous-release-dir", str(previous), "--install-dir", str(root / "install"), "--profile", "gpu"]
if keep: sys.argv.append("--keep")
raise SystemExit(getattr(check, "cli", check.main)())
'''
        for interrupt, keep in itertools.product((signal.SIGINT, signal.SIGTERM), (False, True)):
            with self.subTest(signal=interrupt, keep=keep), tempfile.TemporaryDirectory(prefix="release-interrupt-") as tmp:
                root = Path(tmp)
                candidate = release_fixture(root / "candidate")
                previous = release_fixture(root / "previous", "v1.2.2")
                process = subprocess.Popen(
                    [sys.executable, "-c", program, check.__file__, str(root), str(candidate), str(previous), "keep" if keep else "fresh"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    env=check.safe_environment(), start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 15
                    while not (root / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((root / "ready").exists(), "installer did not start")
                    process.send_signal(interrupt)
                    output, _ = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 130, output)
                    time.sleep(1)
                    self.assertFalse((root / "late").exists(), "installer child survived cancellation")
                    project = (root / "project").read_text()
                    self.assertEqual(check._project_exists(project), keep)
                    self.assertEqual((root / "install").is_dir(), keep)
                    self.assertFalse((root / "install/.release-check-project").exists())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                    ready = root / "ready"
                    if ready.exists():
                        try:
                            installer_pid = int(ready.read_text())
                            command = Path(f"/proc/{installer_pid}/cmdline")
                            if command.exists() and str(root).encode() in command.read_bytes():
                                os.killpg(installer_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if (root / "project").exists():
                        project = (root / "project").read_text()
                        compose = root / "install/deploy/compose"
                        compose.mkdir(parents=True, exist_ok=True)
                        (compose / ".env").write_text(f"COMPOSE_PROJECT_NAME={project}\n")
                        (compose / "compose.yml").write_text('services:\n  worker-rocm:\n    image: postgres:16\n    profiles: [rocm]\n    entrypoint: ["sh", "-c", "exec sleep 120"]\n')
                        check.compose_owned(root / "install", project, ["down", "--timeout", "0", "-v", "--remove-orphans"])

    def test_legacy_model_selection_restores_exact_manifest_bytes(self):
        project = f"release-check-{secrets.token_hex(8)}"
        with tempfile.TemporaryDirectory(prefix="release-models-") as tmp:
            root = Path(tmp)
            models = root / "models"
            models.mkdir()
            before = {check.MODEL + ".json": b"selected", "vega-rt.json": b"realtime", "custom.json": b"operator"}
            for name, data in before.items():
                (models / name).write_bytes(data)
            compose = root / "deploy/compose"
            compose.mkdir(parents=True)
            (compose / ".env").write_text(f"COMPOSE_PROJECT_NAME={project}\n")
            (compose / "compose.yml").write_text(
                'services:\n  worker-rocm:\n    image: postgres:16\n    profiles: [rocm]\n'
                '    entrypoint: ["sh", "-c", "exec sleep 120"]\n'
                f'    volumes: ["{models}:/models"]\n'
            )
            try:
                check.compose_owned(root, project, ["up", "-d", "--no-build", "worker-rocm"])
                check.legacy_model_config(root, project, "rocm")
                self.assertEqual({p.name for p in models.glob("*.json")}, {check.MODEL + ".json"})
                check.legacy_model_config(root, project, "rocm", restore=True)
                self.assertEqual({p.name: p.read_bytes() for p in models.glob("*.json")}, before)
                self.assertFalse((models / ".release-check-manifests").exists())
                check.legacy_model_config(root, project, "rocm", restore=True)
            finally:
                check.compose_owned(root, project, ["down", "--timeout", "0", "-v", "--remove-orphans"])

    def test_guard_survives_install_file_replacement_and_uses_live_reserve(self):
        project = f"release-guard-{secrets.token_hex(6)}-replace"
        with tempfile.TemporaryDirectory(prefix="release-guard-") as temporary:
            root = Path(temporary)
            compose_dir = root / "deploy/compose"
            compose_dir.mkdir(parents=True)
            compose = compose_dir / "compose.yml"
            env = compose_dir / ".env"
            definition = ('services:\n  worker-rocm:\n    image: postgres:16\n'
                          '    profiles: [rocm]\n'
                          '    environment:\n      VRAM_RESERVE_MB: "3072"\n'
                          '      FLEET_TOKEN: guard-env-canary\n'
                          '    entrypoint: ["sh", "-c", "exec sleep 120"]\n')
            settings = f"COMPOSE_PROJECT_NAME={project}\nVRAM_RESERVE_MB=1536\n"
            compose.write_text(definition)
            env.write_text(settings)
            try:
                check.compose_owned(root, project, ["up", "-d", "--no-build", "worker-rocm"])
                worker = check.owned_rocm_worker_ids(project)[0]
                device = root / "sys/card0/device"
                device.mkdir(parents=True)
                for key, value in (("vendor", "0x1002"),
                                   ("mem_info_vram_total", str(8 * 1024**3)),
                                   ("mem_info_vram_used", str(4 * 1024**3))):
                    (device / key).write_text(value)
                compose.write_text("services: [")
                check.rocm_safety_check(project, root / "evidence", root / "sys")
                self.assertEqual((root / "evidence/rocm-reserve-probe.log").read_text().strip(),
                                 "VRAM_RESERVE_MB=3072")
                compose.unlink()
                env.unlink()
                check.rocm_safety_check(project, root / "evidence", root / "sys")
                (device / "mem_info_vram_used").write_text(str(6 * 1024**3))
                with self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                    check.rocm_safety_check(project, root / "evidence", root / "sys")
                state = subprocess.check_output(
                    ["docker", "inspect", "--format", "{{.State.Running}}", worker], text=True,
                ).strip()
                self.assertEqual(state, "false")
            finally:
                compose.write_text(definition)
                env.write_text(settings)
                check.compose_owned(root, project, ["down", "--timeout", "0", "-v", "--remove-orphans"])

    def test_safety_stop_leaves_another_project_running(self):
        with tempfile.TemporaryDirectory(prefix="release-guard-") as temporary:
            root = Path(temporary)
            projects = []
            try:
                for suffix in ("owned", "other"):
                    project = f"release-check-{secrets.token_hex(8)}"
                    directory = root / suffix
                    compose = directory / "deploy/compose"
                    compose.mkdir(parents=True)
                    (compose / "compose.yml").write_text(
                        'services:\n  worker-rocm:\n    image: postgres:16\n'
                        '    profiles: [rocm]\n'
                        '    entrypoint: ["sh", "-c", "exec sleep 120"]\n'
                        '  sentinel:\n    image: postgres:16\n'
                        '    entrypoint: ["sh", "-c", "exec sleep 120"]\n'
                    )
                    (compose / ".env").write_text(
                        f"COMPOSE_PROJECT_NAME={project}\nVRAM_RESERVE_MB=1536\n"
                    )
                    projects.append((directory, project))
                    check.compose_owned(directory, project, ["up", "-d", "--no-build", "worker-rocm", "sentinel"])
                owned, other = projects
                own_ids = check.owned_rocm_worker_ids(owned[1])
                other_ids = check.owned_rocm_worker_ids(other[1])
                self.assertEqual(len(own_ids), 1)
                self.assertEqual(len(other_ids), 1)
                self.assertNotEqual(own_ids, other_ids)
                sentinel = subprocess.check_output([
                    "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={owned[1]}",
                    "--filter", "label=com.docker.compose.service=sentinel",
                ], text=True).strip()
                self.assertTrue(sentinel)
                subprocess.run(["docker", "pause", own_ids[0]], check=True, capture_output=True)
                try:
                    self.assertEqual(check.owned_rocm_worker_ids(owned[1]), [])
                finally:
                    subprocess.run(["docker", "unpause", own_ids[0]], check=True, capture_output=True)
                device = root / "sys/card0/device"
                device.mkdir(parents=True)
                for key, value in (("vendor", "0x1002"),
                                   ("mem_info_vram_total", str(8 * 1024**3)),
                                   ("mem_info_vram_used", str(7 * 1024**3))):
                    (device / key).write_text(value)
                with self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                    check.rocm_safety_check(owned[1], root / "evidence", root / "sys")
                for ids, expected in ((own_ids, "false"), (other_ids, "true"), ([sentinel], "true")):
                    state = subprocess.check_output(
                        ["docker", "inspect", "--format", "{{.State.Running}}", ids[0]], text=True
                    ).strip()
                    self.assertEqual(state, expected)
                (owned[0] / ".release-check-project").write_text(owned[1])
                (owned[0] / "deploy/compose/compose.yml").write_text("services: [")
                try:
                    check.cleanup_rehearsal(owned[0])
                    check.cleanup_rehearsal(owned[0])
                finally:
                    (owned[0] / "deploy/compose/compose.yml").write_text(
                        'services:\n  worker-rocm:\n    image: postgres:16\n    profiles: [rocm]\n'
                        '    entrypoint: ["sh", "-c", "exec sleep 120"]\n'
                    )
                self.assertFalse(check._project_exists(owned[1]))
                self.assertEqual(check.owned_rocm_worker_ids(other[1]), other_ids)
            finally:
                for directory, project in projects:
                    check.run(["docker", "compose", "-p", project,
                               "--env-file", str(directory / "deploy/compose/.env"),
                               "-f", str(directory / "deploy/compose/compose.yml"),
                               "--profile", "*", "down", "--timeout", "0", "-v", "--remove-orphans"])


if __name__ == "__main__":
    unittest.main()
