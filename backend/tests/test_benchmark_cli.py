from __future__ import annotations

import json
import importlib.util
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
SPEC = importlib.util.spec_from_file_location("benchmark", SCRIPTS_DIR / "benchmark.py")
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
with patch.object(sys, "path", [str(SCRIPTS_DIR), *sys.path]):
    SPEC.loader.exec_module(benchmark)


class BenchmarkLoadFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.prompts_path = root / "prompts.json"
        self.matrix_path = root / "matrix.json"
        self.out_dir = root / "out"
        self.prompts_path.write_text(json.dumps([{
            "id": 1,
            "title": "Test scene",
            "category": "test",
            "prompt": "A simple test scene.",
        }]))

        def model(model_id: str) -> dict:
            return {
                "id": model_id,
                "variants": [{"label": "base", "params": {"steps": 2}}],
            }
        self.matrix_path.write_text(json.dumps({
            "models": [model("first"), model("second")],
            "target_vram_gb": 16,
        }))

    def run_main(self, load_side_effect, *, continue_on_error: bool, generation_errors=None):
        argv = [
            "benchmark.py",
            "--prompts", str(self.prompts_path),
            "--matrix", str(self.matrix_path),
            "--out-dir", str(self.out_dir),
        ]
        if continue_on_error:
            argv.append("--continue-on-error")

        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=False)
        response = Mock()
        response.json.return_value = {"id": "session"}
        generation = SimpleNamespace(
            job_id="job-2", width=512, height=512, gpu_ms=900,
        )
        with (
            patch.object(sys, "argv", argv),
            patch.object(benchmark.httpx, "Client", return_value=client),
            patch.object(benchmark, "fetch_registered_models", return_value={"first", "second"}),
            patch.object(benchmark, "require_clean_gpu"),
            patch.object(benchmark, "gpu_status", return_value=[]),
            patch.object(benchmark, "load_model", side_effect=load_side_effect) as load,
            patch.object(benchmark, "unload_all") as unload_all,
            patch.object(benchmark, "unload_model", return_value=[]),
            patch.object(benchmark, "generate_image", return_value=generation,
                         side_effect=generation_errors) as generate,
            patch.object(benchmark.httpx, "post", return_value=response),
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
        ):
            with self.assertRaises(SystemExit) as exit_error:
                benchmark.main()
        return exit_error.exception.code, load, unload_all, generate

    def test_continue_after_http_and_runtime_load_failures_records_cells(self):
        for load_error in (httpx.HTTPError("network unavailable"), RuntimeError("model unsupported")):
            with self.subTest(error=type(load_error).__name__):
                self.out_dir = Path(self.temp_dir.name) / type(load_error).__name__
                load_side_effect = [load_error, 125]
                code, load, unload_all, generate = self.run_main(
                    load_side_effect, continue_on_error=True,
                )

                self.assertEqual(code, 1)
                self.assertEqual([call.args[2] for call in load.call_args_list], ["first", "second"])
                unload_all.assert_called_once()
                generate.assert_called_once()

                summary = json.loads((self.out_dir / "results.json").read_text())
                self.assertEqual((summary["succeeded"], summary["failed"]), (1, 1))
                failed, succeeded = summary["results"]
                self.assertEqual(failed["state"], "failed")
                self.assertEqual(failed["phase"], "load")
                self.assertEqual(failed["error"], benchmark.failure_message(load_error))
                self.assertIsNone(failed["model_load_ms"])
                self.assertNotIn("gpu_ms", failed)
                self.assertNotIn("wall_s", failed)
                self.assertEqual(succeeded["state"], "succeeded")
                self.assertEqual(Path(succeeded["file"]).suffix, ".png")
                self.assertEqual(summary["total_jobs"], 2)
                journal = [json.loads(line) for line in
                           (self.out_dir / "results.jsonl").read_text().splitlines()]
                self.assertEqual(journal, summary["results"])

    def test_failed_load_records_every_prompt_and_variant(self):
        prompts = json.loads(self.prompts_path.read_text())
        prompts.append({**prompts[0], "id": 2, "title": "Second scene"})
        self.prompts_path.write_text(json.dumps(prompts))
        matrix = json.loads(self.matrix_path.read_text())
        for model in matrix["models"]:
            model["variants"].append({"label": "alternate", "params": {"steps": 3}})
        self.matrix_path.write_text(json.dumps(matrix))
        code, _, _, generate = self.run_main(
            [RuntimeError("unsupported"), 125], continue_on_error=True,
        )
        self.assertEqual(code, 1)
        summary = json.loads((self.out_dir / "results.json").read_text())
        self.assertEqual((summary["succeeded"], summary["failed"]), (4, 4))
        self.assertEqual(generate.call_count, 4)
        cells = {(row["prompt_id"], row["variant"]) for row in summary["results"]
                 if row["state"] == "failed"}
        self.assertEqual(cells, {(1, "base"), (1, "alternate"), (2, "base"), (2, "alternate")})

    def test_generation_http_error_is_recorded_and_later_model_runs(self):
        request = httpx.Request("GET", "https://example.invalid/asset?signature=CANARY")
        error = httpx.HTTPStatusError(
            "private CANARY URL", request=request,
            response=httpx.Response(403, request=request),
        )
        success = SimpleNamespace(job_id="job-2", width=512, height=512, gpu_ms=900)
        code, load, _, generate = self.run_main(
            [100, 125], continue_on_error=True, generation_errors=[error, success],
        )
        self.assertEqual(code, 1)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(generate.call_count, 2)
        raw = (self.out_dir / "results.json").read_text()
        self.assertNotIn("CANARY", raw)
        summary = json.loads(raw)
        self.assertEqual((summary["succeeded"], summary["failed"]), (1, 1))
        self.assertEqual(summary["results"][0]["error"], "HTTPStatusError: HTTP 403")
        journal = [json.loads(line) for line in
                   (self.out_dir / "results.jsonl").read_text().splitlines()]
        self.assertEqual(journal, summary["results"])
        html = (self.out_dir / "report.html").read_text()
        self.assertIn("HTTPStatusError: HTTP 403", html)


    def test_load_failure_remains_fail_fast_without_flag(self):
        self.out_dir = Path(self.temp_dir.name) / "fail-fast"
        code, load, unload_all, generate = self.run_main(
            RuntimeError("model unsupported"), continue_on_error=False,
        )

        self.assertNotEqual(code, 0)
        self.assertEqual([call.args[2] for call in load.call_args_list], ["first"])
        unload_all.assert_not_called()
        generate.assert_not_called()
        self.assertFalse((self.out_dir / "results.json").exists())

    def test_existing_journal_is_preserved_and_no_model_is_loaded(self):
        self.out_dir.mkdir()
        journal = self.out_dir / "results.jsonl"
        original = '{"state": "succeeded", "job_id": "previous"}\n'
        journal.write_text(original)
        code, load, _, generate = self.run_main([100, 125], continue_on_error=True)
        self.assertEqual(code, 2)
        load.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(journal.read_text(), original)

def row(model_id: str, state: str, **fields) -> dict:
    base = {
        "prompt_id": 1,
        "title": "Demo prompt",
        "category": "test",
        "model_id": model_id,
        "variant": "v1",
        "cell_key": f"{model_id}__v1",
        "params": {"width": 512, "height": 512, "steps": 4, "seed": 100},
        "model_load_ms": None,
        "state": state,
    }
    base.update(fields)
    return base


class ComputeModelStatsTests(unittest.TestCase):
    def test_counts_states_independently_and_averages_successful_rows_only(self) -> None:
        rows = [
            row("alpha", "succeeded", gpu_ms=1000, wall_s=1.0, model_load_ms=500),
            row("alpha", "succeeded", gpu_ms=3000, wall_s=3.0, model_load_ms=700),
            # A failed attempt records its elapsed time; that time must never
            # enter the averages.
            row("alpha", "failed", wall_s=99.0, error="boom"),
            # A skipped cell is a capacity outcome with no timings of its own.
            row("alpha", "skipped", phase="display_reserve", error="display reserve"),
            row("beta", "skipped", phase="display_reserve", error="display reserve"),
            row("gamma", "failed", error="load failed"),
        ]

        stats = {entry["model_id"]: entry for entry in benchmark.compute_model_stats(rows)}

        alpha = stats["alpha"]
        self.assertEqual(alpha["succeeded"], 2)
        self.assertEqual(alpha["failed"], 1)
        self.assertEqual(alpha["skipped"], 1)
        self.assertEqual(alpha["avg_gpu_ms"], 2000.0)
        self.assertEqual(alpha["median_gpu_ms"], 2000.0)
        self.assertEqual(alpha["avg_wall_s"], 2.0)
        # load_ms is the first successful row's model_load_ms, not a later one.
        self.assertEqual(alpha["load_ms"], 500)

        beta = stats["beta"]
        self.assertEqual(beta["succeeded"], 0)
        self.assertEqual(beta["failed"], 0)
        self.assertEqual(beta["skipped"], 1)
        self.assertIsNone(beta["load_ms"])
        self.assertIsNone(beta["avg_gpu_ms"])
        self.assertEqual(beta["avg_wall_s"], 0.0)

        gamma = stats["gamma"]
        self.assertEqual(gamma["succeeded"], 0)
        self.assertEqual(gamma["failed"], 1)
        self.assertEqual(gamma["skipped"], 0)

        counted = sum(
            entry["succeeded"] + entry["failed"] + entry["skipped"]
            for entry in stats.values()
        )
        self.assertEqual(counted, len(rows))


class ReportCountTests(unittest.TestCase):
    def summary(self) -> dict:
        return {
            "created_at": "2026-10-26T00:00:00+00:00",
            "prompt_count": 1,
            "models": ["demo-model"],
            "variants_per_prompt": 1,
            "target_vram_gb": 16,
            "succeeded": 2,
            "failed": 1,
            "model_stats": [
                {
                    "model_id": "demo-model",
                    "succeeded": 2,
                    "failed": 1,
                    "skipped": 3,
                    "load_ms": 40,
                    "avg_gpu_ms": 100.0,
                    "median_gpu_ms": 110.0,
                    "avg_wall_s": 1.5,
                }
            ],
            "results": [],
            "prompts": [{"id": 1, "title": "Demo prompt", "category": "test", "runs": []}],
        }

    def test_markdown_report_counts_failures_and_skips_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = benchmark.write_report_md(Path(tmp), self.summary())
            text = path.read_text()
        self.assertIn("- **Images:** 2 succeeded, 1 failed, 3 skipped", text)
        self.assertIn("| Model | OK | Fail | Skip |", text)
        self.assertIn("| demo-model | 2 | 1 | 3 | 40 |", text)

    def test_html_report_counts_failures_and_skips_separately(self) -> None:
        summary = self.summary()
        prompts = [{"id": 1, "title": "Demo prompt", "category": "test"}]
        matrix_models = [{"id": "demo-model", "variants": [{"label": "v1", "params": {}}]}]
        with tempfile.TemporaryDirectory() as tmp:
            path = benchmark.write_report_html(Path(tmp), summary, prompts, matrix_models)
            text = path.read_text()
        self.assertIn("2 images, 1 failed, 3 skipped", text)
        self.assertIn("<th>OK</th><th>Fail</th><th>Skip</th>", text)
        self.assertIn("<td>2</td><td>1</td><td>3</td>", text)


if __name__ == "__main__":
    unittest.main()
