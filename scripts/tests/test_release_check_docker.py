import secrets
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.tests.test_release_check import check


class ReleaseGuardDockerTests(unittest.TestCase):
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
        stamp = secrets.token_hex(6)
        with tempfile.TemporaryDirectory(prefix="release-guard-") as temporary:
            root = Path(temporary)
            projects = []
            try:
                for suffix in ("owned", "other"):
                    project = f"release-guard-{stamp}-{suffix}"
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
                check.compose_owned(owned[0], owned[1], ["down", "--timeout", "0", "-v"])
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
