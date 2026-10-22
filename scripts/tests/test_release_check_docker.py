import secrets
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.tests.test_release_check import check


class ReleaseGuardDockerTests(unittest.TestCase):
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
                        '    entrypoint: ["sh", "-c", "exec sleep 120"]\n'
                    )
                    (compose / ".env").write_text(
                        f"COMPOSE_PROJECT_NAME={project}\nVRAM_RESERVE_MB=1536\n"
                    )
                    projects.append((directory, project))
                    check.compose_owned(directory, project, ["up", "-d", "--no-build"])
                owned, other = projects
                own_ids = check.owned_rocm_worker_ids(owned[1])
                other_ids = check.owned_rocm_worker_ids(other[1])
                self.assertEqual(len(own_ids), 1)
                self.assertEqual(len(other_ids), 1)
                self.assertNotEqual(own_ids, other_ids)
                device = root / "sys/card0/device"
                device.mkdir(parents=True)
                for key, value in (("vendor", "0x1002"),
                                   ("mem_info_vram_total", str(8 * 1024**3)),
                                   ("mem_info_vram_used", str(7 * 1024**3))):
                    (device / key).write_text(value)
                with self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                    check.rocm_safety_check(owned[0], owned[1], root / "evidence", root / "sys")
                for ids, expected in ((own_ids, "false"), (other_ids, "true")):
                    state = subprocess.check_output(
                        ["docker", "inspect", "--format", "{{.State.Running}}", ids[0]], text=True
                    ).strip()
                    self.assertEqual(state, expected)
            finally:
                for directory, project in projects:
                    check.compose_owned(directory, project, ["down", "--timeout", "0", "-v", "--remove-orphans"])


if __name__ == "__main__":
    unittest.main()
