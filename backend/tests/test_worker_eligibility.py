"""Which connected workers may be given work, sessions, models or plain GPU commands.

Two rules decide every site: a worker holds a current work grant (jobs,
realtime sessions, listed models), and a worker accepts plain GPU commands
(studio and benchmark). These tests pin both for each kind of worker, with
no database.
"""

import time
import uuid
from unittest.mock import MagicMock

import pytest
from starlette.websockets import WebSocketState

from app import jobs, realtime, registry
from app.manifests import Manifest

MODEL_ID = "eligibility-model"

# (name, Worker kwargs, holds a grant, accepts plain GPU commands)
WORKER_KINDS = [
    pytest.param(
        {"protocol_version": 5, "incarnation": None},
        True, True,
        id="protocol5_always_holds_grant",
    ),
    pytest.param(
        {"protocol_version": 6, "incarnation": uuid.uuid4(),
         "admission_ready": True, "grant_deadline": time.monotonic() + 3600},
        True, False,
        id="protocol6_current_grant",
    ),
    pytest.param(
        {"protocol_version": 6, "incarnation": uuid.uuid4(),
         "admission_ready": True, "grant_deadline": None},
        True, False,
        id="protocol6_grant_without_deadline",
    ),
    pytest.param(
        {"protocol_version": 6, "incarnation": uuid.uuid4(),
         "admission_ready": False, "grant_deadline": time.monotonic() + 3600},
        False, False,
        id="protocol6_not_admission_ready",
    ),
    pytest.param(
        {"protocol_version": 6, "incarnation": uuid.uuid4(),
         "admission_ready": True, "grant_deadline": time.monotonic() - 1},
        False, False,
        id="protocol6_grant_expired",
    ),
]


@pytest.fixture
def connected():
    """Register one worker in realtime.workers for the test, then restore."""
    saved = dict(realtime.workers)
    realtime.workers.clear()

    def register(kwargs):
        socket = MagicMock(client_state=WebSocketState.CONNECTED)
        worker = realtime.Worker(
            id=f"w-{uuid.uuid4()}",
            ws=socket,
            manifests=[Manifest(id=MODEL_ID, name=MODEL_ID,
                                capabilities=["text_to_image"])],
            realtime_slots=1,
            **kwargs,
        )
        realtime.workers[worker.id] = worker
        return worker

    yield register
    realtime.workers.clear()
    realtime.workers.update(saved)


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_grant_is_held_only_by_protocol5_or_a_current_protocol6_grant(kwargs, grant, plain):
    worker = realtime.Worker(id="w-grant", ws=None, manifests=[], realtime_slots=1, **kwargs)
    assert worker.has_current_work_grant is grant


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_only_a_worker_without_incarnation_accepts_plain_gpu_commands(kwargs, grant, plain):
    worker = realtime.Worker(id="w-plain", ws=None, manifests=[], realtime_slots=1, **kwargs)
    assert realtime.accepts_plain_gpu_commands(worker) is plain


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_job_dispatch_picks_a_worker_only_while_it_holds_a_grant(connected, kwargs, grant, plain):
    worker = connected(kwargs)
    picked = jobs.pick_job_worker(MODEL_ID)
    assert (picked is worker) is grant


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_realtime_session_picks_a_worker_only_while_it_holds_a_grant(connected, kwargs, grant, plain):
    worker = connected(kwargs)
    assert (realtime.pick_worker(MODEL_ID) is worker) is grant


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_model_is_known_only_while_its_worker_holds_a_grant(connected, kwargs, grant, plain):
    connected(kwargs)
    assert realtime.model_known(MODEL_ID) is grant


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_model_is_listed_only_while_its_worker_holds_a_grant(connected, kwargs, grant, plain):
    connected(kwargs)
    assert (MODEL_ID in registry.available()) is grant


@pytest.mark.parametrize(("kwargs", "grant", "plain"), WORKER_KINDS)
def test_plain_gpu_command_picks_go_only_to_a_worker_without_incarnation(connected, kwargs, grant, plain):
    worker = connected(kwargs)
    assert (realtime.pick_any_worker() is worker) is plain
    assert (realtime.pick_worker_for_model(MODEL_ID) is worker) is plain
