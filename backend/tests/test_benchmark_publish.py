from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PUBLISHER = Path(__file__).resolve().parents[2] / "scripts" / "publish-benchmark.py"


class BenchmarkPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "input.json"
        self.destination = self.root / "public" / "nested" / "results.json"

    def publish(self, payload: str) -> subprocess.CompletedProcess[str]:
        self.source.write_text(payload, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(PUBLISHER), str(self.source), str(self.destination)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_removes_operational_data_and_preserves_public_measurements(self):
        report = {
            "created_at": "2026-10-28T00:00:00Z",
            "succeeded": 1,
            "failed": 0,
            "skipped": 1,
            "unknown_internal": "drop this",
            "api": "http://local",
            "out_dir": "/tmp/run",
            "prompts": [{
                "id": 2,
                "prompt": "A sample scene",
                "runs": [{
                    "model_id": "m1",
                    "variant": "v1",
                    "state": "succeeded",
                    "gpu_ms": 1234,
                    "wall_s": 2.5,
                    "params": {"steps": 6, "seed": 100},
                    "job_id": "j1",
                    "cell_key": "m1__v1",
                    "api": "http://local",
                    "out_dir": "/tmp/run",
                    "file": "/benchmark/images/example.png",
                }, {
                    "model_id": "m1",
                    "state": "skipped",
                    "file": "/tmp/run/private.png",
                }],
            }],
            "model_revisions": {"m1": "rev1"},
            "publication": {
                "methodology_url": "/benchmark/methodology.md",
                "results_url": "/benchmark/results.json",
            },
        }

        result = self.publish(json.dumps(report))

        self.assertEqual(result.returncode, 0, result.stderr)
        public = json.loads(self.destination.read_text(encoding="utf-8"))
        self.assertNotIn("unknown_internal", public)
        self.assertNotIn("api", public)
        self.assertNotIn("out_dir", public)
        self.assertEqual(public["succeeded"], 1)
        self.assertEqual(public["failed"], 0)
        self.assertEqual(public["skipped"], 1)
        self.assertEqual(public["prompts"][0]["id"], 2)
        successful, skipped = public["prompts"][0]["runs"]
        self.assertEqual(successful["gpu_ms"], 1234)
        self.assertEqual(successful["wall_s"], 2.5)
        self.assertEqual(successful["params"], {"steps": 6, "seed": 100})
        self.assertEqual(successful["file"], "/benchmark/images/example.png")
        self.assertNotIn("job_id", successful)
        self.assertNotIn("cell_key", successful)
        self.assertNotIn("api", successful)
        self.assertNotIn("out_dir", successful)
        self.assertNotIn("file", skipped)
        self.assertEqual(public["model_revisions"], {"m1": "rev1"})
        self.assertEqual(public["publication"], report["publication"])

    def test_non_object_json_does_not_overwrite_existing_destination(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("keep", encoding="utf-8")

        result = self.publish("[]")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("top-level object", result.stderr)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
