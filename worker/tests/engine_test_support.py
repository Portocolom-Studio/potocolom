from types import SimpleNamespace

from worker.engine import DiffusersEngine
from worker.frame_batch import FrameBatchCollector


class _OutOfMemoryError(RuntimeError):
    pass


class _TestDiffusersEngine(DiffusersEngine):
    @staticmethod
    def _import_torch():
        return SimpleNamespace(
            float16="float16",
            float32="float32",
            OutOfMemoryError=_OutOfMemoryError,
        )


def make_engine():
    engine = _TestDiffusersEngine("cpu")
    engine._batch_collector = FrameBatchCollector(engine, window_ms=0.0)
    return engine
