import builtins
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from scripts.tests.test_release_check import check


class RocmGpuGuardTests(unittest.TestCase):
    def setUp(self):
        check.rocm_worker_card.cache_clear()
        self.addCleanup(check.rocm_worker_card.cache_clear)

    @staticmethod
    def card(root, name, pci, total=16 * 1024**3, used=2 * 1024**3):
        device = root / name / "device"
        device.mkdir(parents=True)
        for field, value in {"vendor": "0x1002", "uevent": f"PCI_SLOT_NAME={pci}\n",
                             "mem_info_vram_total": str(total), "mem_info_vram_used": str(used)}.items():
            (device / field).write_text(value, encoding="ascii")
        return device

    def test_secondary_small_card_does_not_stop_selected_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.card(root / "sys", "card0", "0000:02:00.0", 1024**3, 900 * 1024**2)
            selected = self.card(root / "sys", "card1", "0000:03:00.0")

            def docker(args, **_kwargs):
                if args[1] == "inspect":
                    return "VRAM_RESERVE_MB=1536\n"
                if args[-1] == check.ROCM_GPU_ID_SCRIPT:
                    return "torch startup notice\nPOTOCOLOM_GPU_PCI=0000:03:00\n"
                return ""

            with patch.object(check, "owned_rocm_worker_ids", return_value=["0123456789ab"]), \
                    patch.object(check, "run", side_effect=docker), \
                    patch.object(check, "stop_owned_rocm_workers") as stop:
                check.rocm_safety_check("owned", root / "private", root / "sys")
                stop.assert_not_called()
                (selected / "mem_info_vram_used").write_text(str(15 * 1024**3))
                with self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                    check.rocm_safety_check("owned", root / "private", root / "sys")
                stop.assert_called_once_with("owned", root / "private")
                evidence = (root / "private/rocm-safety-evidence.log").read_text()
                self.assertIn("card1:", evidence)
                self.assertNotIn("card0:", evidence)

    def test_each_selected_card_uses_its_own_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, free in (("card0", 2), ("card1", 4)):
                self.card(root / "sys", name, f"0000:0{2 + int(name[-1])}:00.0",
                          used=(16 - free) * 1024**3)

            def probe(worker, _project, args, _log):
                if args[1] == "inspect":
                    return f"VRAM_RESERVE_MB={1536 if worker == 'a' else 3072}\n"
                return ""

            with patch.object(check, "owned_rocm_worker_ids", return_value=["a", "b"]), \
                    patch.object(check, "rocm_worker_probe", side_effect=probe), \
                    patch.object(check, "rocm_worker_card", side_effect=["card0", "card1"]), \
                    patch.object(check, "stop_owned_rocm_workers") as stop:
                check.rocm_safety_check("owned", root / "private", root / "sys")
            stop.assert_not_called()

    def test_selected_gpu_without_vram_stats_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.card(root / "sys", "card0", "0000:02:00.0")
            with patch.object(check, "owned_rocm_worker_ids", return_value=["a"]), \
                    patch.object(check, "rocm_worker_probe", return_value=""), \
                    patch.object(check, "rocm_worker_card", return_value="card1"), \
                    self.assertRaisesRegex(check.CheckError, "no reportable physical VRAM"):
                check.rocm_safety_check("owned", root / "private", root / "sys")

    def test_workers_on_same_gpu_keep_larger_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.card(root / "sys", "card0", "0000:03:00.0", used=14 * 1024**3)

            def probe(worker, _project, args, _log):
                if args[1] == "inspect":
                    return f"VRAM_RESERVE_MB={3072 if worker == 'a' else 1536}\n"
                return ""

            with patch.object(check, "owned_rocm_worker_ids", return_value=["a", "b"]), \
                    patch.object(check, "rocm_worker_probe", side_effect=probe), \
                    patch.object(check, "rocm_worker_card", return_value="card0"), \
                    patch.object(check, "stop_owned_rocm_workers") as stop, \
                    self.assertRaisesRegex(check.CheckError, "VRAM reserve breached"):
                check.rocm_safety_check("owned", root / "private", root / "sys")
            stop.assert_called_once_with("owned", root / "private")

    def test_gpu_identity_cache_follows_container_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.card(root, "card1", "0000:03:00.0")
            with patch.object(check, "rocm_worker_probe", return_value="POTOCOLOM_GPU_PCI=0000:03:00\n") as probe:
                for worker in ("a", "a", "b"):
                    self.assertEqual(check.rocm_worker_card(worker, "owned", root / "log", root), "card1")
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(probe.call_args.args[2][:4], ["docker", "exec", "b", "python"])

    def test_stopped_worker_is_not_cached_as_a_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.card(root, "card0", "0000:03:00.0")
            with patch.object(check, "rocm_worker_probe", side_effect=[None, "POTOCOLOM_GPU_PCI=0000:03:00\n"]):
                with self.assertRaises(check._WorkerStopped):
                    check.rocm_worker_card("a", "owned", root / "log", root)
                self.assertEqual(check.rocm_worker_card("a", "owned", root / "log", root), "card0")

    def test_missing_malformed_and_multiple_pci_markers_fail_closed(self):
        for output in ("", "POTOCOLOM_GPU_PCI=not-a-pci\n",
                       "POTOCOLOM_GPU_PCI=0000:03:00\n" * 2):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(check, "rocm_worker_probe", return_value=output), \
                        self.assertRaisesRegex(check.CheckError, "one valid GPU PCI address"):
                    check.rocm_worker_card("a", "owned", root / "log", root)

    def test_unmatched_and_ambiguous_physical_gpu_fail_closed(self):
        for slots in ([], ["0000:04:00.0"], ["0000:03:00.0", "0000:03:00.1"]):
            with self.subTest(slots=slots), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for index, slot in enumerate(slots):
                    self.card(root, f"card{index}", slot)
                with patch.object(check, "rocm_worker_probe", return_value="POTOCOLOM_GPU_PCI=0000:03:00\n"), \
                        self.assertRaisesRegex(check.CheckError, "one physical AMD card"):
                    check.rocm_worker_card("a", "owned", root / "log", root)

    def test_probe_uses_worker_selector_and_legacy_cuda_device(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                gpu = SimpleNamespace(pci_domain_id=0, pci_bus_id=3, pci_device_id=0)
                properties = Mock(return_value=gpu)
                torch = SimpleNamespace(version=SimpleNamespace(hip="6.3"),
                                        cuda=SimpleNamespace(get_device_properties=properties))
                module = ModuleType("worker.gpu_device")
                module.select_device = Mock(return_value="cuda:1")
                worker = ModuleType("worker")
                worker.__path__ = []
                with patch.dict(sys.modules, {"torch": torch, "worker": worker,
                                              "worker.gpu_device": None if legacy else module}), \
                        patch.dict(os.environ, {"DEVICE": "rocm"}), redirect_stdout(io.StringIO()) as output:
                    exec(check.ROCM_GPU_ID_SCRIPT, {})
                properties.assert_called_once_with("cuda" if legacy else "cuda:1")
                if not legacy:
                    module.select_device.assert_called_once_with(torch, "rocm")
                self.assertEqual(output.getvalue(), "POTOCOLOM_GPU_PCI=0000:03:00\n")

    def test_probe_rejects_non_hip_and_cpu_workers(self):
        for hip, device in ((None, "cuda:0"), ("6.3", "cpu")):
            with self.subTest(hip=hip, device=device):
                torch = SimpleNamespace(version=SimpleNamespace(hip=hip))
                module = ModuleType("worker.gpu_device")
                module.select_device = Mock(return_value=device)
                with patch.dict(sys.modules, {"torch": torch, "worker.gpu_device": module}), \
                        self.assertRaisesRegex(RuntimeError, "HIP build|GPU inference"):
                    exec(check.ROCM_GPU_ID_SCRIPT, {})

    def test_probe_does_not_hide_broken_selector_dependency(self):
        real_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == "worker.gpu_device":
                raise ModuleNotFoundError("missing selector dependency", name="broken_dependency")
            return real_import(name, *args, **kwargs)

        torch = SimpleNamespace(version=SimpleNamespace(hip="6.3"))
        with patch.dict(sys.modules, {"torch": torch}), \
                patch("builtins.__import__", side_effect=import_module), \
                self.assertRaisesRegex(ModuleNotFoundError, "selector dependency"):
            exec(check.ROCM_GPU_ID_SCRIPT, {})
