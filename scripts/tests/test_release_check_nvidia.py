import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.tests.test_release_check import DRIVER, release_fixture


class NvidiaReleaseCheckTests(unittest.TestCase):
    def test_ssh_wrapper_rejects_injection_before_gh_or_ssh(self) -> None:
        wrapper = DRIVER.with_name("release-check-nvidia.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            marker = root / "side-effect"
            for name in ("gh", "ssh"):
                tool = bin_dir / name
                tool.write_text(f"#!/bin/bash\ntouch {marker}\nexit 99\n", encoding="utf-8")
                tool.chmod(0o755)
            for host, version, path in (("-oProxyCommand=bad@host", "v1.2.3", ""),
                                  ("user@host;touch /tmp/bad", "v1.2.3", ""),
                                  ("user@host", "v1.2.3;touch", ""),
                                  ("user@host", "v1.2.3", "relative/path")):
                result = subprocess.run(["bash", str(wrapper)], env={**os.environ,
                                             "PATH": f"{bin_dir}:{os.environ['PATH']}",
                                             "HOST": host, "VERSION": version,
                                             "INSTALL_DIR": path},
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(marker.exists())

    def test_ssh_wrapper_quotes_remote_directory_and_transfers_only_inputs(self) -> None:
        wrapper = DRIVER.with_name("release-check-nvidia.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            capture = root / "ssh-args"
            members = root / "members"
            (bin_dir / "gh").write_text(
                "#!/bin/bash\n"
                "while (($#)); do if [[ $1 == --dir ]]; then target=$2; break; fi; shift; done\n"
                "mkdir -p \"$target\"\n"
                "printf bundle >\"$target/potocolom-$VERSION-selfhost.tar.gz\"\n"
                "printf '#!/bin/bash\\n' >\"$target/install.sh\"\n"
                "sha256sum \"$target/install.sh\" \"$target/potocolom-$VERSION-selfhost.tar.gz\" | sed \"s#${target}/##g\" > \"$target/SHA256SUMS\"\n",
                encoding="utf-8")
            (bin_dir / "ssh").write_text(
                "#!/bin/bash\n[[ -z \"${GH_TOKEN:-}${GITHUB_TOKEN:-}${GH_ENTERPRISE_TOKEN:-}\" ]] || exit 99\n"
                "printf '%s\\n' \"$@\" >\"$SSH_CAPTURE\"\n"
                "tar -tf - >\"$TAR_MEMBERS\"\n",
                encoding="utf-8")
            (bin_dir / "gh").chmod(0o755)
            (bin_dir / "ssh").chmod(0o755)
            install_dir = "/tmp/potocolom check 'single' \"double\"; printf injected"
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "HOST": "leon@host.example", "VERSION": "v1.2.3",
                   "GH_TOKEN": "local-only-token",
                   "INSTALL_DIR": install_dir, "SSH_CAPTURE": str(capture),
                   "TAR_MEMBERS": str(members)}
            result = subprocess.run(["bash", str(wrapper)], env=env,
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            remote_command = capture.read_text(encoding="utf-8").splitlines()[-1]
            self.assertRegex(remote_command, r"potocolom\\+ check")
            self.assertIn("single", remote_command)
            self.assertIn("double", remote_command)
            self.assertEqual(set(members.read_text(encoding="utf-8").splitlines()),
                             {"install.sh", "potocolom-v1.2.3-selfhost.tar.gz",
                              "SHA256SUMS", "release-check.py"})

    def test_ssh_wrapper_default_uses_remote_home(self) -> None:
        wrapper = DRIVER.with_name("release-check-nvidia.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            capture = root / "ssh-args"
            (bin_dir / "gh").write_text(
                "#!/bin/bash\n"
                "while (($#)); do if [[ $1 == --dir ]]; then target=$2; break; fi; shift; done\n"
                "mkdir -p \"$target\"; printf bundle >\"$target/potocolom-$VERSION-selfhost.tar.gz\"\n"
                "printf '#!/bin/bash\\n' >\"$target/install.sh\"\n"
                "sha256sum \"$target/install.sh\" \"$target/potocolom-$VERSION-selfhost.tar.gz\" | sed \"s#${target}/##g\" > \"$target/SHA256SUMS\"\n",
                encoding="utf-8")
            (bin_dir / "ssh").write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" >\"$SSH_CAPTURE\"; tar -tf - >/dev/null\n",
                                         encoding="utf-8")
            for tool in (bin_dir / "gh", bin_dir / "ssh"):
                tool.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": "/local/home",
                   "HOST": "leon@host.example", "VERSION": "v1.2.3", "SSH_CAPTURE": str(capture)}
            result = subprocess.run(["bash", str(wrapper)], env=env, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            command = capture.read_text(encoding="utf-8").splitlines()[-1]
            self.assertIn('"$HOME/potocolom"', command)
            self.assertNotIn("/local/home", command)

    def test_ssh_wrapper_local_artifacts_skip_gh(self) -> None:
        wrapper = DRIVER.with_name("release-check-nvidia.sh")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release = release_fixture(root / "local artifacts")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh_marker = root / "gh-called"
            (bin_dir / "gh").write_text(f"#!/bin/bash\ntouch {gh_marker}\nexit 99\n", encoding="utf-8")
            (bin_dir / "ssh").write_text("#!/bin/bash\ntar -tf - >/dev/null\n", encoding="utf-8")
            (bin_dir / "gh").chmod(0o755)
            (bin_dir / "ssh").chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "HOST": "leon@host.example", "VERSION": "v1.2.3",
                   "RELEASE_DIR": str(release)}
            result = subprocess.run(["bash", str(wrapper)], env=env, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(gh_marker.exists())


if __name__ == "__main__":
    unittest.main()
