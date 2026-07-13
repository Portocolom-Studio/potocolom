import asyncio

from worker.engine import SimulatedEngine
from worker.manifests import SIMULATED_MANIFEST


def test_simulated_gpu_lifecycle():
    engine = SimulatedEngine(0.04)

    async def scenario():
        assert engine.loaded_models() == []
        load_ms = await engine.load_model(SIMULATED_MANIFEST)
        assert load_ms >= 0
        assert engine.loaded_models() == ["sd-sim"]
        await engine.unload_all()
        assert engine.loaded_models() == []

    asyncio.run(scenario())
