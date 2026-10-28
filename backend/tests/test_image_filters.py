"""Model, date and action filters on the Images library: the
/api/v1/generations listing, its new /models options route, and the
roots_only ancestor walk that keeps a tree when a descendant matches."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import db
from app.main import app
from app.tables import Asset, Job, Model, User

FLEET_HEADERS = {"x-fleet-token": "test-fleet-token"}

BASE = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)


async def _seed_model(session, model_id: str, capabilities: list[str]) -> None:
    # Models are not drained between tests, so every caller passes a unique id.
    if await session.get(Model, model_id) is None:
        session.add(Model(
            id=model_id,
            name=f"Model {model_id}",
            capabilities=capabilities,
            parameters_schema={},
            min_vram_gb=0,
        ))
        await session.flush()


async def _seed_job(
    session,
    *,
    user_id: uuid.UUID,
    model_id: str,
    created_at: datetime,
    prompt: str,
    capabilities: list[str] | tuple[str, ...] = ("text_to_image",),
    source_asset_id: uuid.UUID | None = None,
    starred_at: datetime | None = None,
    with_asset: bool = True,
    asset_dimensions: tuple[int, int] = (512, 512),
    asset_expires_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    await _seed_model(session, model_id, list(capabilities))
    job_id = uuid.uuid4()
    session.add(Job(
        id=job_id,
        user_id=user_id,
        model_id=model_id,
        params={"prompt": prompt},
        state="succeeded",
        attempt=1,
        source_asset_id=source_asset_id,
        starred_at=starred_at,
        created_at=created_at,
    ))
    await session.flush()
    if not with_asset:
        return job_id, None
    asset_id = uuid.uuid4()
    session.add(Asset(
        id=asset_id,
        user_id=user_id,
        job_id=job_id,
        parent_asset_id=source_asset_id,
        storage_key=f"{user_id}/{job_id}.png",
        mime="image/png",
        width=asset_dimensions[0],
        height=asset_dimensions[1],
        expires_at=asset_expires_at,
    ))
    await session.flush()
    session.add(Asset(
        user_id=user_id,
        job_id=job_id,
        parent_asset_id=asset_id,
        storage_key=f"{user_id}/{job_id}-thumb.webp",
        mime="image/webp",
        width=384,
        height=384,
    ))
    await session.flush()
    return job_id, asset_id


async def _seed_foreign_user(session) -> uuid.UUID:
    foreign_id = uuid.uuid4()
    session.add(User(id=foreign_id, email=f"{foreign_id}@example.com", role="user"))
    await session.flush()
    return foreign_id


def _ids(response) -> list[str]:
    assert response.status_code == 200
    return [row["id"] for row in response.json()]


def _option_id(client, prefix: str) -> str:
    options = client.get("/api/v1/generations/models").json()["models"]
    return next(option["id"] for option in options if option["id"].startswith(prefix))


@pytest.mark.db
def test_generation_models_options_are_owner_scoped_and_complete():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, str]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            models = {
                "current": f"models-current-{suffix}",
                "benchmark": f"models-benchmark-{suffix}",
                "assetless": f"models-assetless-{suffix}",
                "foreign": f"models-foreign-{suffix}",
            }
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                # A model no studio offers any more must stay filterable.
                await _seed_job(
                    session, user_id=db.local_user_id, model_id=models["current"],
                    created_at=BASE, prompt="kept", capabilities=["benchmark"],
                )
                await _seed_job(
                    session, user_id=db.local_user_id, model_id=models["benchmark"],
                    created_at=BASE, prompt="old benchmark run",
                    capabilities=["benchmark"],
                )
                # A job with no saved image contributes no option.
                await _seed_job(
                    session, user_id=db.local_user_id, model_id=models["assetless"],
                    created_at=BASE, prompt="failed before saving", with_asset=False,
                )
                await _seed_job(
                    session, user_id=foreign_id, model_id=models["foreign"],
                    created_at=BASE, prompt="someone else's",
                )
                await session.commit()
            return models

        models = client.portal.call(seed)
        body = client.get("/api/v1/generations/models")
        assert body.status_code == 200
        options = body.json()["models"]
        ids = [option["id"] for option in options]
        assert models["current"] in ids
        assert models["benchmark"] in ids
        assert models["assetless"] not in ids
        assert models["foreign"] not in ids
        assert ids == sorted(ids)
        assert all(option["name"] for option in options)


@pytest.mark.db
def test_generation_history_repeated_model_id_selects_the_union():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            model_ids = {name: f"union-{name}-{suffix}" for name in ("a", "b", "c")}
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                for position, name in enumerate(model_ids):
                    job_id, _ = await _seed_job(
                        session, user_id=db.local_user_id, model_id=model_ids[name],
                        created_at=BASE - timedelta(seconds=position), prompt=name,
                    )
                    ids[name] = job_id
                # The foreign job shares model a; it must never appear.
                await _seed_job(
                    session, user_id=foreign_id, model_id=model_ids["a"],
                    created_at=BASE, prompt="foreign",
                )
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        options = client.get("/api/v1/generations/models").json()["models"]
        by_kind = {option["id"].split("-")[1]: option["id"] for option in options}
        chosen = client.get("/api/v1/generations", params=[
            ("model_id", by_kind["a"]), ("model_id", by_kind["b"]),
        ])
        assert _ids(chosen) == [str(ids["a"]), str(ids["b"])]
        assert str(ids["c"]) not in _ids(chosen)


@pytest.mark.db
def test_generation_history_rejects_a_model_id_that_cannot_be_stored():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        response = client.get("/api/v1/generations?model_id=%00")
        assert response.status_code == 422
        assert "cannot be stored" in response.text


@pytest.mark.db
def test_generation_history_date_bounds_are_inclusive_and_exclusive():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            model_id = f"bounds-{uuid.uuid4()}"
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                for name, offset in (("at_from", 0), ("inside", 1), ("at_before", 2)):
                    job_id, _ = await _seed_job(
                        session, user_id=db.local_user_id, model_id=model_id,
                        created_at=BASE + timedelta(hours=offset), prompt=name,
                    )
                    ids[name] = job_id
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        window = client.get("/api/v1/generations", params={
            "created_from": BASE.isoformat(),
            "created_before": (BASE + timedelta(hours=2)).isoformat(),
        })
        # created_from includes its instant, created_before excludes its own.
        assert _ids(window) == [str(ids["inside"]), str(ids["at_from"])]


@pytest.mark.db
def test_generation_history_window_validation_rejects_naive_and_reversed():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        assert client.get("/api/v1/generations").status_code == 200
        naive = client.get(
            "/api/v1/generations", params={"created_from": "2026-03-10T12:00:00"},
        )
        assert naive.status_code == 422
        date_only = client.get(
            "/api/v1/generations", params={"created_from": "2026-03-10"},
        )
        assert date_only.status_code == 422
        reversed_ = client.get("/api/v1/generations", params={
            "created_from": "2026-03-11T00:00:00+00:00",
            "created_before": "2026-03-10T00:00:00+00:00",
        })
        assert reversed_.status_code == 422
        equal = client.get("/api/v1/generations", params={
            "created_from": "2026-03-10T00:00:00+00:00",
            "created_before": "2026-03-10T00:00:00+00:00",
        })
        assert equal.status_code == 422


@pytest.mark.db
def test_generation_history_action_filter_reads_the_model_row():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            plain = f"act-plain-{suffix}"
            upscaler = f"act-upscale-{suffix}"
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                root_id, root_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=plain,
                    created_at=BASE, prompt="plain root",
                    capabilities=["text_to_image", "image_to_image"],
                )
                ids["generate"] = root_id
                edit_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=plain,
                    created_at=BASE + timedelta(seconds=1), prompt="edit",
                    capabilities=["text_to_image", "image_to_image"],
                    source_asset_id=root_asset,
                )
                ids["image_to_image"] = edit_id
                upscale_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=upscaler,
                    created_at=BASE + timedelta(seconds=2), prompt="upscale",
                    capabilities=["image_to_image", "upscale"],
                    source_asset_id=root_asset,
                )
                ids["upscale"] = upscale_id
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        for action, expected in ids.items():
            page = client.get(
                "/api/v1/generations", params={"action": action, "limit": 50},
            )
            assert _ids(page) == [str(expected)]
        unknown = client.get("/api/v1/generations", params={"action": "upload"})
        assert unknown.status_code == 422


@pytest.mark.db
def test_generation_history_filters_combine_with_starred_and_prompt():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            keep = f"combo-keep-{suffix}"
            other = f"combo-other-{suffix}"
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                hit_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=keep,
                    created_at=BASE, prompt="castle at dawn", starred_at=BASE,
                )
                ids["hit"] = hit_id
                # Same model and prompt, outside the window.
                old_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=keep,
                    created_at=BASE - timedelta(days=40), prompt="castle at dusk",
                    starred_at=BASE,
                )
                ids["old"] = old_id
                # Inside the window and starred, other model.
                other_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=other,
                    created_at=BASE, prompt="castle at noon", starred_at=BASE,
                )
                ids["other"] = other_id
                # Same model and window, unstarred.
                plain_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=keep,
                    created_at=BASE, prompt="castle at night",
                )
                ids["plain"] = plain_id
                # Matching every constraint but the prompt.
                lighthouse_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=keep,
                    created_at=BASE, prompt="lighthouse", starred_at=BASE,
                )
                ids["lighthouse"] = lighthouse_id
                # A foreign account matching every constraint.
                foreign_job, _ = await _seed_job(
                    session, user_id=foreign_id, model_id=keep,
                    created_at=BASE, prompt="castle at dawn", starred_at=BASE,
                )
                ids["foreign"] = foreign_job
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        keep_model = _option_id(client, "combo-keep-")
        page = client.get("/api/v1/generations", params={
            "model_id": keep_model,
            "created_from": (BASE - timedelta(days=7)).isoformat(),
            "created_before": (BASE + timedelta(days=1)).isoformat(),
            "starred": "true",
            "q": "castle",
        })
        assert _ids(page) == [str(ids["hit"])]
        for excluded in ("foreign", "old", "other", "plain", "lighthouse"):
            assert str(ids[excluded]) not in _ids(page)


@pytest.mark.db
def test_generation_history_filter_reaches_past_the_first_page():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> uuid.UUID:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            filler = f"page-filler-{suffix}"
            buried = f"page-buried-{suffix}"
            async with db.session_factory() as session:
                for offset in range(60):
                    await _seed_job(
                        session, user_id=db.local_user_id, model_id=filler,
                        created_at=BASE + timedelta(seconds=offset),
                        prompt=f"filler {offset}", with_asset=False,
                    )
                buried_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=buried,
                    created_at=BASE - timedelta(days=400), prompt="long buried",
                )
                await session.commit()
            return buried_id

        buried_id = client.portal.call(seed)
        first_page = _ids(client.get("/api/v1/generations", params={"limit": 50}))
        assert len(first_page) == 50
        assert str(buried_id) not in first_page
        # The filter lives in SQL, so the buried row is one page away, not sixty.
        filtered = client.get(
            "/api/v1/generations", params={"model_id": _option_id(client, "page-buried-")},
        )
        assert _ids(filtered) == [str(buried_id)]
        windowed = client.get(
            "/api/v1/generations", params={"created_before": BASE.isoformat()},
        )
        assert _ids(windowed) == [str(buried_id)]


@pytest.mark.db
def test_generation_history_cursor_must_match_the_active_filters():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            chosen = f"cursor-chosen-{suffix}"
            skipped = f"cursor-skipped-{suffix}"
            async with db.session_factory() as session:
                newest_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=chosen,
                    created_at=BASE, prompt="newest",
                )
                older_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=skipped,
                    created_at=BASE - timedelta(seconds=1), prompt="older",
                )
                await session.commit()
            return newest_id, older_id

        newest_id, older_id = client.portal.call(seed)
        chosen_model = _option_id(client, "cursor-chosen-")
        # The unfiltered anchor sits under another model; under this filter it
        # is not a member, so the page is refused rather than mixed.
        refused = client.get("/api/v1/generations", params={
            "model_id": chosen_model, "cursor": str(older_id),
        })
        assert refused.status_code == 404
        accepted = client.get("/api/v1/generations", params={
            "model_id": chosen_model, "cursor": str(newest_id), "limit": 10,
        })
        assert _ids(accepted) == []
        # The anchor must also sit inside the active date window.
        refused_by_date = client.get("/api/v1/generations", params={
            "created_from": (BASE + timedelta(hours=1)).isoformat(),
            "cursor": str(newest_id),
        })
        assert refused_by_date.status_code == 404
        accepted_by_date = client.get("/api/v1/generations", params={
            "created_from": (BASE - timedelta(hours=1)).isoformat(),
            "cursor": str(older_id),
        })
        assert accepted_by_date.status_code == 200


@pytest.mark.db
def test_generation_history_roots_filter_keeps_ancestors_of_matches():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            root_model = f"roots-root-{suffix}"
            mid_model = f"roots-mid-{suffix}"
            leaf_model = f"roots-leaf-{suffix}"
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                root_id, root_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=root_model,
                    created_at=BASE, prompt="root outside the filter",
                )
                ids["root"] = root_id
                mid_id, mid_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=mid_model,
                    created_at=BASE + timedelta(seconds=1), prompt="middle",
                    source_asset_id=root_asset,
                )
                ids["mid"] = mid_id
                leaf_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=leaf_model,
                    created_at=BASE + timedelta(seconds=2), prompt="leaf",
                    source_asset_id=mid_asset,
                )
                ids["leaf"] = leaf_id
                lonely_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=mid_model,
                    created_at=BASE - timedelta(seconds=1), prompt="lonely root",
                )
                ids["lonely"] = lonely_id
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        # The leaf matches two levels below its root: the tree is kept whole.
        by_leaf = client.get("/api/v1/generations", params={
            "roots_only": "true", "model_id": _option_id(client, "roots-leaf-"),
        })
        assert _ids(by_leaf) == [str(ids["root"])]
        # The middle is not a root either; its own tree comes in through it and
        # the unrelated lonely root stays out.
        by_mid = client.get("/api/v1/generations", params={
            "roots_only": "true", "model_id": _option_id(client, "roots-mid-"),
        })
        assert _ids(by_mid) == [str(ids["root"]), str(ids["lonely"])]
        assert str(ids["leaf"]) not in _ids(by_mid)
        # Without filters the page is unchanged: both roots, newest first.
        plain = client.get("/api/v1/generations", params={"roots_only": "true"})
        assert _ids(plain) == [str(ids["root"]), str(ids["lonely"])]


@pytest.mark.db
def test_generation_history_ancestor_walk_stays_inside_the_owner():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            leaf_model = f"hop-leaf-{suffix}"
            other_model = f"hop-other-{suffix}"
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                # A foreign asset whose producing job is foreign: the walk must
                # stop at the asset rather than climb into that tree.
                foreign_root, foreign_asset = await _seed_job(
                    session, user_id=foreign_id, model_id=other_model,
                    created_at=BASE, prompt="foreign root",
                )
                ids["foreign_root"] = foreign_root
                own_root, own_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=other_model,
                    created_at=BASE, prompt="own root",
                )
                ids["own_root"] = own_root
                bridge_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=leaf_model,
                    created_at=BASE + timedelta(seconds=1), prompt="bridge",
                    source_asset_id=own_asset,
                )
                ids["bridge"] = bridge_id
                crosser_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=leaf_model,
                    created_at=BASE + timedelta(seconds=2), prompt="crosser",
                    source_asset_id=foreign_asset,
                )
                ids["crosser"] = crosser_id
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        page = client.get("/api/v1/generations", params={
            "roots_only": "true", "model_id": _option_id(client, "hop-leaf-"),
        })
        assert _ids(page) == [str(ids["own_root"])]
        assert str(ids["foreign_root"]) not in _ids(page)


@pytest.mark.db
def test_generation_history_does_not_bridge_through_a_foreign_owned_source_asset():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> str:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            root_model = f"foreign-source-root-{suffix}"
            leaf_model = f"foreign-source-leaf-{suffix}"
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                _, root_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=root_model,
                    created_at=BASE, prompt="owned producer with reassigned asset",
                )
                assert root_asset is not None
                # Malformed legacy relationship: the owned root produced this
                # asset, but the asset row now belongs to another user.
                await session.execute(
                    update(Asset).where(Asset.id == root_asset).values(user_id=foreign_id)
                )
                await _seed_job(
                    session, user_id=db.local_user_id, model_id=leaf_model,
                    created_at=BASE + timedelta(seconds=1), prompt="filtered leaf",
                    source_asset_id=root_asset,
                )
                await session.commit()
            return leaf_model

        leaf_model = client.portal.call(seed)
        page = client.get("/api/v1/generations", params={
            "roots_only": "true", "model_id": leaf_model,
        })
        assert _ids(page) == []


@pytest.mark.db
def test_generation_history_does_not_traverse_a_foreign_parent_job():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> str:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            root_model = f"foreign-parent-root-{suffix}"
            intermediate_model = f"foreign-parent-middle-{suffix}"
            leaf_model = f"foreign-parent-leaf-{suffix}"
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                _, root_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=root_model,
                    created_at=BASE, prompt="owned ancestor outside filter",
                )
                assert root_asset is not None
                _, intermediate_asset = await _seed_job(
                    session, user_id=foreign_id, model_id=intermediate_model,
                    created_at=BASE + timedelta(seconds=1), prompt="foreign intermediate",
                    source_asset_id=root_asset,
                )
                assert intermediate_asset is not None
                # The source asset is owned but its producer job is foreign;
                # that job points to the owned root and must not be a bridge.
                await session.execute(
                    update(Asset)
                    .where(Asset.id == intermediate_asset)
                    .values(user_id=db.local_user_id)
                )
                await _seed_job(
                    session, user_id=db.local_user_id, model_id=leaf_model,
                    created_at=BASE + timedelta(seconds=2), prompt="filtered leaf",
                    source_asset_id=intermediate_asset,
                )
                await session.commit()
            return leaf_model

        leaf_model = client.portal.call(seed)
        page = client.get("/api/v1/generations", params={
            "roots_only": "true", "model_id": leaf_model,
        })
        assert _ids(page) == []


@pytest.mark.db
def test_generation_history_ancestor_walk_terminates_on_a_cycle(monkeypatch):
    monkeypatch.setattr(db, "DB_STATEMENT_TIMEOUT_MS", 2000)
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            model_id = f"cycle-{uuid.uuid4()}"
            first, second = uuid.uuid4(), uuid.uuid4()
            async with db.session_factory() as session:
                await _seed_model(session, model_id, ["text_to_image"])
                # An unmatched root forces PostgreSQL to evaluate the ancestor
                # set; with only cyclic non-roots, it can skip the walk entirely.
                await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"outside-cycle-{uuid.uuid4()}",
                    created_at=BASE, prompt="unmatched root",
                )
                session.add(Job(
                    id=first, user_id=db.local_user_id, model_id=model_id,
                    params={"prompt": "cycle one"}, state="succeeded", attempt=1,
                    created_at=BASE,
                ))
                session.add(Job(
                    id=second, user_id=db.local_user_id, model_id=model_id,
                    params={"prompt": "cycle two"}, state="succeeded", attempt=1,
                    created_at=BASE + timedelta(seconds=1),
                ))
                await session.flush()
                first_asset, second_asset = uuid.uuid4(), uuid.uuid4()
                session.add(Asset(
                    id=first_asset, user_id=db.local_user_id, job_id=first,
                    storage_key=f"{db.local_user_id}/{first}.png",
                    mime="image/png", width=512, height=512,
                ))
                session.add(Asset(
                    id=second_asset, user_id=db.local_user_id, job_id=second,
                    storage_key=f"{db.local_user_id}/{second}.png",
                    mime="image/png", width=512, height=512,
                ))
                await session.flush()
                # Each job cites the other's output: a walk without a distinct
                # union would recurse until the database stopped it.
                await session.execute(
                    update(Job).where(Job.id == first).values(source_asset_id=second_asset)
                )
                await session.execute(
                    update(Job).where(Job.id == second).values(source_asset_id=first_asset)
                )
                await session.commit()
            return first, second

        first, second = client.portal.call(seed)
        page = client.get("/api/v1/generations", params={
            "roots_only": "true", "model_id": _option_id(client, "cycle-"),
        })
        assert page.status_code == 200
        assert page.json() == []


@pytest.mark.db
def test_generation_history_filters_apply_to_the_ids_view():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> tuple[uuid.UUID, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            keep = f"ids-keep-{suffix}"
            skip = f"ids-skip-{suffix}"
            async with db.session_factory() as session:
                keep_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=keep,
                    created_at=BASE, prompt="keep",
                )
                skip_id, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=skip,
                    created_at=BASE, prompt="skip",
                )
                await session.commit()
            return keep_id, skip_id

        keep_id, skip_id = client.portal.call(seed)
        body = client.get("/api/v1/generations", params={
            "fields": "ids", "model_id": _option_id(client, "ids-keep-"), "limit": 5000,
        }).json()
        assert body["ids"] == [str(keep_id)]
        assert str(skip_id) not in body["ids"]


@pytest.mark.db
def test_generation_history_size_bins_cover_each_boundary_and_portrait():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, list[uuid.UUID]]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            model_id = f"sizes-boundary-{uuid.uuid4()}"
            cases = [
                (1, 1, "512"), (639, 1, "512"),
                (640, 1, "768"), (895, 1, "768"),
                (896, 1, "1k"), (1535, 1, "1k"),
                (1536, 1, "2k"), (3071, 1, "2k"),
                (3072, 1, "4k"), (6143, 1, "4k"),
                (6144, 1, "above_4k"), (768, 1344, "1k"),
            ]
            expected: dict[str, list[uuid.UUID]] = {}
            async with db.session_factory() as session:
                for index, (width, height, size) in enumerate(cases):
                    job_id, _ = await _seed_job(
                        session, user_id=db.local_user_id, model_id=model_id,
                        created_at=BASE + timedelta(seconds=index),
                        prompt=f"boundary {index}", asset_dimensions=(width, height),
                    )
                    expected.setdefault(size, []).append(job_id)
                await session.commit()
            return expected

        expected = client.portal.call(seed)
        model_id = _option_id(client, "sizes-boundary-")
        for size, ids in expected.items():
            response = client.get("/api/v1/generations", params={
                "size": size, "model_id": model_id,
            })
            actual = set(_ids(response))
            assert actual == {str(job_id) for job_id in ids}


@pytest.mark.db
def test_generation_history_size_matches_any_owned_master_and_unknown_is_owner_scoped():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                foreign_id = await _seed_foreign_user(session)
                multi, first_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE, prompt="multi", asset_dimensions=(512, 512),
                )
                ids["multi"] = multi
                assert first_asset is not None
                session.add(Asset(
                    user_id=db.local_user_id, job_id=multi,
                    storage_key=f"{db.local_user_id}/{multi}-large.png",
                    mime="image/png", width=2560, height=1920,
                ))
                no_asset, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=1), prompt="no asset", with_asset=False,
                )
                ids["assetless"] = no_asset
                thumbnail_only, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=2), prompt="thumbnail", with_asset=False,
                )
                ids["thumbnail"] = thumbnail_only
                session.add(Asset(
                    user_id=db.local_user_id, job_id=thumbnail_only,
                    storage_key=f"{db.local_user_id}/{thumbnail_only}-thumb.webp",
                    mime="image/webp", width=384, height=384,
                ))
                invalid_width, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=3), prompt="zero width", with_asset=False,
                )
                ids["invalid_width"] = invalid_width
                session.add(Asset(
                    user_id=db.local_user_id, job_id=invalid_width,
                    storage_key=f"{db.local_user_id}/{invalid_width}.png",
                    mime="image/png", width=0, height=2048,
                ))
                invalid_height, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=4), prompt="negative height", with_asset=False,
                )
                ids["invalid_height"] = invalid_height
                session.add(Asset(
                    user_id=db.local_user_id, job_id=invalid_height,
                    storage_key=f"{db.local_user_id}/{invalid_height}.png",
                    mime="image/png", width=2048, height=-1,
                ))
                foreign_master, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=5), prompt="foreign master", with_asset=False,
                )
                ids["foreign_master"] = foreign_master
                session.add(Asset(
                    user_id=foreign_id, job_id=foreign_master,
                    storage_key=f"{foreign_id}/{foreign_master}.png",
                    mime="image/png", width=4096, height=3072,
                ))
                expired, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=6), prompt="expired metadata",
                    asset_dimensions=(1344, 768),
                    asset_expires_at=BASE - timedelta(days=1),
                )
                ids["expired"] = expired
                partial_expired, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-cases-{suffix}",
                    created_at=BASE + timedelta(seconds=7), prompt="partially expired outputs",
                    asset_dimensions=(512, 512),
                )
                ids["partial_expired"] = partial_expired
                session.add(Asset(
                    user_id=db.local_user_id, job_id=partial_expired,
                    storage_key=f"{db.local_user_id}/{partial_expired}-large.png",
                    mime="image/png", width=2560, height=1920,
                    expires_at=BASE - timedelta(days=1),
                ))
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        model_id = _option_id(client, "size-cases-")
        params = {"size": "unknown", "model_id": model_id}
        unknown = set(_ids(client.get("/api/v1/generations", params=params)))
        assert unknown == {
            str(ids["assetless"]), str(ids["thumbnail"]),
            str(ids["invalid_width"]), str(ids["invalid_height"]),
            str(ids["foreign_master"]),
        }
        size_page = client.get("/api/v1/generations", params={"size": "2k", "model_id": model_id})
        assert _ids(size_page) == [str(ids["partial_expired"]), str(ids["multi"])]
        partial_row = size_page.json()[0]
        assert partial_row["expired"] is False
        assert [(asset["width"], asset["height"]) for asset in partial_row["assets"]] == [(512, 512)]
        assert _ids(client.get("/api/v1/generations", params={"size": "4k", "model_id": model_id})) == []
        # A second matching master does not duplicate the job; the same job
        # also matches either selected bin. Repeated values are deduplicated.
        both = client.get("/api/v1/generations", params=[
            ("size", "512"), ("size", "512"), ("size", "2k"),
            ("model_id", model_id),
        ])
        assert _ids(both).count(str(ids["multi"])) == 1
        assert _ids(client.get("/api/v1/generations", params={"size": "1k", "model_id": model_id})) == [
            str(ids["expired"]),
        ]


@pytest.mark.db
def test_generation_history_size_combines_with_existing_filters_and_lineage():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> dict[str, uuid.UUID]:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            root_model = f"size-root-{suffix}"
            upscale_model = f"size-upscale-{suffix}"
            other_model = f"size-other-{suffix}"
            lineage_model = f"size-lineage-{suffix}"
            ids: dict[str, uuid.UUID] = {}
            async with db.session_factory() as session:
                root, root_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=root_model,
                    created_at=BASE, prompt="root outside size",
                )
                assert root_asset is not None
                ids["root"] = root
                _, action_asset = await _seed_job(
                    session, user_id=db.local_user_id, model_id=f"size-action-root-{suffix}",
                    created_at=BASE - timedelta(seconds=1), prompt="action source",
                )
                assert action_asset is not None
                keep, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=upscale_model,
                    created_at=BASE + timedelta(seconds=1), prompt="violet keep",
                    capabilities=["text_to_image", "upscale"],
                    source_asset_id=action_asset,
                    starred_at=BASE, asset_dimensions=(2560, 1920),
                )
                ids["keep"] = keep
                wrong_size, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=upscale_model,
                    created_at=BASE + timedelta(seconds=2), prompt="violet size",
                    capabilities=["text_to_image", "upscale"], starred_at=BASE,
                    asset_dimensions=(1344, 768),
                )
                ids["wrong_size"] = wrong_size
                wrong_model, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=other_model,
                    created_at=BASE + timedelta(seconds=3), prompt="violet model",
                    starred_at=BASE, asset_dimensions=(2560, 1920),
                )
                ids["wrong_model"] = wrong_model
                wrong_star, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=upscale_model,
                    created_at=BASE + timedelta(seconds=4), prompt="violet star",
                    asset_dimensions=(2560, 1920),
                )
                ids["wrong_star"] = wrong_star
                wrong_query, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=upscale_model,
                    created_at=BASE + timedelta(seconds=5), prompt="amber query",
                    starred_at=BASE, asset_dimensions=(2560, 1920),
                )
                ids["wrong_query"] = wrong_query
                wrong_action, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=other_model,
                    created_at=BASE + timedelta(seconds=6), prompt="violet action",
                    starred_at=BASE, asset_dimensions=(2560, 1920),
                )
                ids["wrong_action"] = wrong_action
                lineage_leaf, _ = await _seed_job(
                    session, user_id=db.local_user_id, model_id=lineage_model,
                    created_at=BASE + timedelta(seconds=7), prompt="lineage 1k",
                    source_asset_id=root_asset, asset_dimensions=(768, 1344),
                )
                ids["lineage_leaf"] = lineage_leaf
                await session.commit()
            return ids

        ids = client.portal.call(seed)
        model = _option_id(client, "size-upscale-")
        page = client.get("/api/v1/generations", params={
            "size": "2k", "model_id": model,
            "created_from": BASE.isoformat(),
            "created_before": (BASE + timedelta(minutes=1)).isoformat(),
            "starred": "true", "q": "violet", "action": "upscale",
        })
        assert _ids(page) == [str(ids["keep"])]

        roots = client.get("/api/v1/generations", params={
            "roots_only": "true", "size": "1k",
            "model_id": _option_id(client, "size-lineage-"),
        })
        assert _ids(roots) == [str(ids["root"])]
        rejected_root_cursor = client.get("/api/v1/generations", params={
            "roots_only": "true", "size": "2k",
            "model_id": _option_id(client, "size-lineage-"),
            "cursor": str(ids["root"]),
        })
        assert rejected_root_cursor.status_code == 404

        rejected_cross_size = client.get("/api/v1/generations", params={
            "size": "2k", "cursor": str(ids["wrong_size"]),
        })
        assert rejected_cross_size.status_code == 404


@pytest.mark.db
def test_generation_history_size_filter_applies_to_ids_and_buried_matches():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed() -> uuid.UUID:
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            async with db.session_factory() as session:
                for offset in range(60):
                    await _seed_job(
                        session, user_id=db.local_user_id,
                        model_id=f"size-fill-{suffix}",
                        created_at=BASE + timedelta(seconds=offset),
                        prompt=f"filler {offset}", with_asset=False,
                    )
                buried, _ = await _seed_job(
                    session, user_id=db.local_user_id,
                    model_id=f"size-buried-{suffix}",
                    created_at=BASE - timedelta(days=400), prompt="buried 4k",
                    asset_dimensions=(4096, 3072),
                )
                await session.commit()
            return buried

        buried = client.portal.call(seed)
        assert str(buried) not in _ids(client.get("/api/v1/generations", params={"limit": 50}))
        filtered = client.get("/api/v1/generations", params={"size": "4k"})
        assert _ids(filtered) == [str(buried)]
        ids_view = client.get("/api/v1/generations", params={
            "fields": "ids", "size": "4k", "limit": 5000,
        })
        assert ids_view.status_code == 200
        assert ids_view.json()["ids"] == [str(buried)]


@pytest.mark.db
def test_generation_history_rejects_unknown_size_enum():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        response = client.get("/api/v1/generations", params={"size": "1536"})
        assert response.status_code == 422


@pytest.mark.db
def test_generation_history_state_limits_matching_descendants():
    with TestClient(app, headers=FLEET_HEADERS) as client:
        async def seed():
            assert db.local_user_id is not None
            assert db.session_factory is not None
            suffix = uuid.uuid4()
            result = {}
            model = f"state-descendant-{suffix}"
            async with db.session_factory() as session:
                for label, state in [("saved", "succeeded"), ("failed", "failed")]:
                    root, source = await _seed_job(
                        session, user_id=db.local_user_id, model_id=f"state-root-{suffix}",
                        created_at=BASE, prompt=f"{label} root",
                    )
                    child, _ = await _seed_job(
                        session, user_id=db.local_user_id, model_id=model,
                        created_at=BASE + timedelta(seconds=1), prompt=f"{label} child",
                        source_asset_id=source, with_asset=state == "succeeded",
                    )
                    if state == "failed":
                        await session.execute(update(Job).where(Job.id == child).values(state=state))
                    result[label] = (root, child)
                await session.commit()
            return model, result

        model, ids = client.portal.call(seed)
        params = {"roots_only": "true", "model_id": model}
        all_states = set(_ids(client.get("/api/v1/generations", params=params)))
        assert all_states == {str(ids["saved"][0]), str(ids["failed"][0])}
        succeeded = client.get("/api/v1/generations", params={**params, "state": "succeeded"})
        assert _ids(succeeded) == [str(ids["saved"][0])]
        assert client.get("/api/v1/generations", params={
            **params, "state": "succeeded", "cursor": str(ids["failed"][0]),
        }).status_code == 404
