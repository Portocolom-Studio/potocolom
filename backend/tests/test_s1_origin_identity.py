"""Origin-identity guard for the update_session mirror callback (S1, P2).

The callback `mirror_desired` built inside `realtime.send_v6_command` decides
whether a committed desired snapshot may be written back into the RAM session.
That decision must be keyed on the ORIGINAL command fields -- the generation
and attempt the update was created for -- and not on the RAM generation and
attempt at the moment the send runs: a resume between origin and send leaves a
newer generation in RAM, and a stale committed snapshot must not be mirrored
into it.

Source-level guard test only. The command-store commit and the delivery lower
operation are replaced with doubles; no PostgreSQL, no worker socket and no
physical delivery are involved, and the test claims none of them.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import realtime, worker_authority

COMMITTED_PARAMS = {"prompt": "older direct command"}
COMMITTED_REVISION = 2
CURRENT_PARAMS = {"prompt": "current"}


async def _drive(monkeypatch, *, origin_generation, origin_attempt,
                 ram_generation, ram_attempt, replace_desired=False):
    """Run the real send_v6_command against a RAM session whose generation and
    attempt may differ from the originating command fields.

    The commit double stands in for a KNOWN committed outcome of the original
    command: it invokes the callback the real send_v6_command built, with the
    committed snapshot and revision. The delivery double calls the prepare the
    real send supplies, so the actual helper and the actual callback run; only
    the storage and transport below them are absent.
    """
    account = uuid.uuid4()
    session_id = uuid.uuid4()
    worker = realtime.Worker(
        f"s1-origin-{uuid.uuid4()}", None, [], 1, protocol_version=6,
        incarnation=uuid.uuid4(), grant_nonce=uuid.uuid4(), admission_ready=True,
        transport_owner_id=worker_authority.TRANSPORT_OWNER_ID,
    )
    session = realtime.Session(
        session_id, "model", None, params=dict(CURRENT_PARAMS),
        desired_params=dict(CURRENT_PARAMS), desired_revision=1,
        user_id=account, state="live", control_generation=ram_generation,
        attempt_id=ram_attempt,
    )
    snapshot = session.desired_params
    committed_snapshots = []

    async def commit(worker_id, incarnation, kind, fields, **kwargs):
        assert kind == "update_session"
        desired = kwargs.get("desired_committed")
        if replace_desired:
            session.desired_params = dict(session.desired_params)
        committed_snapshots.append((dict(COMMITTED_PARAMS), COMMITTED_REVISION))
        if desired is not None:
            desired(dict(COMMITTED_PARAMS), COMMITTED_REVISION)
        return SimpleNamespace(sequence=2)

    async def deliver(worker, *, prepare=None):
        assert prepare is not None, "send_v6_command must supply its prepare to the delivery"
        return await prepare()

    fields = {
        "session_id": str(session_id),
        "attempt_id": str(origin_attempt),
        "control_generation": origin_generation,
        "model_id": "model",
        "params": dict(COMMITTED_PARAMS),
        "params_revision": COMMITTED_REVISION,
    }
    context = {
        "auth_session_id": None,
        "browser_owner_id": worker_authority.TRANSPORT_OWNER_ID,
        "attempt_id": origin_attempt,
        "control_generation": origin_generation,
        "model_id": "model",
        "base_desired_revision": 1,
        "desired_revision": COMMITTED_REVISION,
        "params": dict(COMMITTED_PARAMS),
    }
    monkeypatch.setitem(realtime.workers, worker.id, worker)
    monkeypatch.setitem(realtime.sessions, session_id, session)
    with patch("app.command_store.commit_command", commit), \
            patch("app.realtime._deliver_pending_worker_command", deliver):
        result = await realtime.send_v6_command(
            worker, "update_session", fields,
            account_user_id=account, session_context=context,
        )
    return SimpleNamespace(session=session, snapshot=snapshot, result=result,
                           committed_snapshots=committed_snapshots)


@pytest.mark.parametrize(
    "ram_generation,ram_attempt_new,expect_applied",
    [
        (2, False, False),
        (1, True, False),
        (1, False, True),
    ],
    ids=["generation_mismatch", "attempt_mismatch", "matching_origin"],
)
def test_mirror_callback_keys_on_the_original_command_fields(
        monkeypatch, ram_generation, ram_attempt_new, expect_applied):
    async def run():
        origin_attempt = uuid.uuid4()
        ram_attempt = uuid.uuid4() if ram_attempt_new else origin_attempt
        outcome = await _drive(
            monkeypatch, origin_generation=1, origin_attempt=origin_attempt,
            ram_generation=ram_generation, ram_attempt=ram_attempt,
        )
        assert outcome.committed_snapshots == [(dict(COMMITTED_PARAMS), COMMITTED_REVISION)], (
            "the actual prepare must reach the known committed outcome")
        assert outcome.result is not None and outcome.result.sequence == 2, (
            "the send must complete through the real prepare path")
        session = outcome.session
        assert session.control_generation == ram_generation
        assert session.attempt_id is ram_attempt
        if expect_applied:
            assert session.desired_params == dict(COMMITTED_PARAMS), (
                "a command whose origin still matches RAM must mirror the "
                f"committed params, got {session.desired_params!r}")
            assert session.desired_revision == COMMITTED_REVISION, (
                "a mirrored command must carry the committed revision, "
                f"got {session.desired_revision!r}")
        else:
            assert session.desired_params is outcome.snapshot, (
                "a stale-origin commit must not touch the newer generation's "
                "desired object")
            assert session.desired_params == dict(CURRENT_PARAMS)
            assert session.desired_revision == 1, (
                "a stale-origin commit must not advance the newer generation's "
                f"revision, got {session.desired_revision!r}")

    asyncio.run(run())


def test_mirror_callback_skips_a_replaced_desired_object(monkeypatch):
    """Generation and attempt still match the original fields, but another
    update replaced the desired object between capture and commit: the object
    identity guard must refuse the write."""
    async def run():
        origin_attempt = uuid.uuid4()
        outcome = await _drive(
            monkeypatch, origin_generation=1, origin_attempt=origin_attempt,
            ram_generation=1, ram_attempt=origin_attempt, replace_desired=True,
        )
        assert outcome.committed_snapshots == [(dict(COMMITTED_PARAMS), COMMITTED_REVISION)]
        session = outcome.session
        assert session.desired_params == dict(CURRENT_PARAMS), (
            "a replaced desired object must not be overwritten by the commit")
        assert session.desired_revision == 1, (
            "a refused mirror must leave the revision alone, "
            f"got {session.desired_revision!r}")

    asyncio.run(run())
