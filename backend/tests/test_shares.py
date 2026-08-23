import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import db, sessions
from app.main import app
from app.tables import Asset, AssetShare, Job, Model
from tests.test_totp_flow import ORIGIN, _csrf, _login, _make, accounts

MODEL = "sd-share"

__all__ = ["accounts"]


@pytest.fixture
def library(accounts):
    """The accounts fixture empties users, and an asset row would hold one down."""
    yield accounts

    async def clear() -> None:
        async with db.session_factory() as session:
            for table in ("asset_shares", "assets", "jobs"):
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()

    if db.session_factory is None:
        accounts(db.connect())
    accounts(clear())
    accounts(db.dispose())


async def _owned_asset(user_id: uuid.UUID, prompt: str = "a red house on a hill") -> Asset:
    async with db.session_factory() as session:
        if await session.get(Model, MODEL) is None:
            session.add(Model(id=MODEL, name="SD Share", capabilities=["text_to_image"],
                              parameters_schema={}, min_vram_gb=0))
            await session.flush()
        job = Job(id=uuid.uuid4(), user_id=user_id, model_id=MODEL,
                  params={"prompt": prompt}, state="succeeded")
        session.add(job)
        await session.flush()
        asset = Asset(id=uuid.uuid4(), user_id=user_id, job_id=job.id,
                      storage_key=f"{user_id}/shared.png", mime="image/png",
                      width=64, height=64)
        session.add(asset)
        await session.commit()
        return asset


async def _shares() -> list[AssetShare]:
    async with db.session_factory() as session:
        return list((await session.execute(select(AssetShare))).scalars().all())


def _share(client, asset_id, days=7):
    return client.post("/api/v1/shares", headers=_csrf(client),
                       json={"asset_id": str(asset_id), "days": days})


def _resolve(client, token):
    return client.post("/api/v1/shared", headers={"Origin": ORIGIN}, json={"token": token})


def _token_of(url: str) -> str:
    return url.split("#", 1)[1]


@pytest.mark.db
def test_a_share_is_a_fragment_link_and_only_its_hash_is_kept(library):
    """The token never reaches the server in a path or a query, so it cannot
    land in an access log, a Referer, or browser history as a URL."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "sharer@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "sharer@example.com").status_code == 204
        made = _share(client, asset.id)
        assert made.status_code == 201
        url = made.json()["url"]
        assert url.startswith(f"{ORIGIN}/shared#")
        row = client.portal.call(_shares)[0]
    assert _token_of(url).encode() not in row.token_hash


@pytest.mark.db
@pytest.mark.parametrize("days", [1, 7, 30])
def test_the_offered_lifetimes_are_one_seven_and_thirty_days(library, days):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, f"life{days}@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, f"life{days}@example.com").status_code == 204
        before = datetime.now(timezone.utc)
        assert _share(client, asset.id, days).status_code == 201
        row = client.portal.call(_shares)[0]
    window = row.expires_at - before
    assert timedelta(days=days) <= window < timedelta(days=days) + timedelta(minutes=1)


@pytest.mark.db
@pytest.mark.parametrize("days", [0, 2, 90, -1])
def test_any_other_lifetime_is_refused(library, days):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, f"bad{days}@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, f"bad{days}@example.com").status_code == 204
        assert _share(client, asset.id, days).status_code == 422


@pytest.mark.db
def test_only_the_owner_can_share_an_asset(library):
    with TestClient(app, base_url=ORIGIN) as client:
        owner = client.portal.call(_make, "owner12@example.com")
        asset = client.portal.call(_owned_asset, owner.id)
        client.portal.call(_make, "stranger@example.com")
        assert _login(client, "stranger@example.com").status_code == 204
        assert _share(client, asset.id).status_code == 404
        assert client.portal.call(_shares) == []


@pytest.mark.db
def test_a_share_resolves_without_any_credential_and_stays_reusable(library):
    """The link is the capability. It works for whoever holds it, again and
    again, until it is revoked or expires."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "reuse@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "reuse@example.com").status_code == 204
        token = _token_of(_share(client, asset.id).json()["url"])
    with TestClient(app, base_url=ORIGIN) as anyone:
        first = _resolve(anyone, token)
        assert first.status_code == 200
        assert _resolve(anyone, token).status_code == 200
        assert anyone.get("/api/v1/account").status_code == 401


@pytest.mark.db
def test_the_answer_carries_the_picture_and_nothing_about_who_made_it(library):
    """A share is one image, not a window onto the account behind it."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "allowlist@example.com")
        asset = client.portal.call(_owned_asset, user.id, "a cat in a hat")
        assert _login(client, "allowlist@example.com").status_code == 204
        token = _token_of(_share(client, asset.id).json()["url"])
    with TestClient(app, base_url=ORIGIN) as anyone:
        body = _resolve(anyone, token).json()
    assert set(body) == {"asset", "prompt", "model", "url"}
    assert set(body["asset"]) == {"id", "width", "height", "mime"}
    assert body["prompt"] == "a cat in a hat"
    assert body["model"] == MODEL
    flat = str(body)
    assert "allowlist@example.com" not in flat
    assert str(user.id) not in flat
    assert asset.storage_key not in flat


@pytest.mark.db
def test_the_picture_url_lasts_sixty_seconds(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "sixty@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "sixty@example.com").status_code == 204
        token = _token_of(_share(client, asset.id).json()["url"])
    with TestClient(app, base_url=ORIGIN) as anyone:
        from app import shares

        assert shares.PICTURE_TTL == 60
        assert _resolve(anyone, token).json()["url"]


@pytest.mark.db
def test_a_revoked_share_stops_resolving(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "revoker@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "revoker@example.com").status_code == 204
        made = _share(client, asset.id).json()
        token = _token_of(made["url"])
        assert _resolve(client, token).status_code == 200
        assert client.delete(f"/api/v1/shares/{made['id']}",
                             headers=_csrf(client)).status_code == 204
        assert _resolve(client, token).status_code == 404


@pytest.mark.db
def test_an_expired_share_stops_resolving(library):
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "expiry@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "expiry@example.com").status_code == 204
        token = _token_of(_share(client, asset.id).json()["url"])

        async def age():
            async with db.session_factory() as session:
                await session.execute(text("UPDATE asset_shares SET expires_at = :past"),
                                      {"past": datetime.now(timezone.utc) - timedelta(minutes=1)})
                await session.commit()

        client.portal.call(age)
        assert _resolve(client, token).status_code == 404


@pytest.mark.db
def test_sharing_again_replaces_the_link_it_had(library):
    """One active share per asset. The old link must stop working, or
    revoking the visible one would leave an invisible one alive."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "replacer@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "replacer@example.com").status_code == 204
        first = _token_of(_share(client, asset.id).json()["url"])
        second = _token_of(_share(client, asset.id, 30).json()["url"])
        assert first != second
        assert _resolve(client, first).status_code == 404
        assert _resolve(client, second).status_code == 200


@pytest.mark.db
def test_an_unknown_token_answers_the_same_as_a_revoked_one(library):
    with TestClient(app, base_url=ORIGIN) as anyone:
        unknown = _resolve(anyone, "not-a-share-token")
    assert unknown.status_code == 404


@pytest.mark.db
def test_only_the_owner_can_revoke(library):
    with TestClient(app, base_url=ORIGIN) as client:
        owner = client.portal.call(_make, "owner13@example.com")
        asset = client.portal.call(_owned_asset, owner.id)
        assert _login(client, "owner13@example.com").status_code == 204
        made = _share(client, asset.id).json()
    with TestClient(app, base_url=ORIGIN) as other:
        other.portal.call(_make, "meddler@example.com")
        assert _login(other, "meddler@example.com").status_code == 204
        assert other.delete(f"/api/v1/shares/{made['id']}",
                            headers=_csrf(other)).status_code == 404
        assert _resolve(other, _token_of(made["url"])).status_code == 200


@pytest.mark.db
def test_there_is_no_way_to_fetch_a_share_by_url(library):
    """A GET with the token in the path or the query would put it in every
    access log and every Referer between here and the viewer."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "noget@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "noget@example.com").status_code == 204
        token = _token_of(_share(client, asset.id).json()["url"])
        assert client.get(f"/api/v1/shared/{token}").status_code == 404
        assert client.get("/api/v1/shared", params={"token": token}).status_code == 405


@pytest.mark.db
def test_the_retired_asset_column_is_left_alone(library):
    """assets.share_token is the old design. Writing it would give two
    answers to the question of whether an asset is shared."""
    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "retired@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "retired@example.com").status_code == 204
        _share(client, asset.id)

        async def stored() -> str | None:
            async with db.session_factory() as session:
                return (await session.execute(
                    select(Asset.share_token).where(Asset.id == asset.id))).scalar_one()

        assert client.portal.call(stored) is None


@pytest.mark.db
def test_two_requests_sharing_one_asset_leave_one_link(library):
    """Both revoke what they read and insert their own, and the index decides.
    Two live links for one asset would mean revoking the visible one leaves
    the other working."""
    import asyncio

    from fastapi import HTTPException

    from app import shares

    with TestClient(app, base_url=ORIGIN) as client:
        user = client.portal.call(_make, "racer12@example.com")
        asset = client.portal.call(_owned_asset, user.id)
        assert _login(client, "racer12@example.com").status_code == 204
        principal = client.portal.call(sessions.resolve,
                                       next(c.value for c in client.cookies.jar
                                            if c.name.endswith("potocolom_session")))
        body = shares.ShareRequest(asset_id=asset.id, days=7)

        async def both():
            async with db.session_factory() as one, db.session_factory() as two:
                return await asyncio.gather(
                    shares.share(body, principal.user, one),
                    shares.share(body, principal.user, two),
                    return_exceptions=True,
                )

        outcomes = client.portal.call(both)
        live = [row for row in client.portal.call(_shares) if row.revoked_at is None]
    assert len(live) == 1
    assert all(not isinstance(o, Exception) or isinstance(o, HTTPException) for o in outcomes)
