"""Images filters keep the authenticated owner in accounts mode."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update

from app import db
from app.main import app
from app.tables import Asset, Job, Model, User
from tests.test_image_filters import BASE, _ids, _seed_job
from tests.test_totp_flow import ORIGIN, _login, _make, accounts

__all__ = ["accounts"]


@pytest.mark.db
@pytest.mark.parametrize("role", ["viewer", "user", "admin"])
def test_image_filters_require_session_and_keep_owner_for_every_role(accounts, role):
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/generations?size=2k").status_code == 401
        assert client.get("/api/v1/generations/models").status_code == 401
        suffix = uuid.uuid4()
        own_email = f"images-{role}-{suffix}@example.com"
        own = client.portal.call(_make, own_email, role)
        other = client.portal.call(_make, f"images-other-{suffix}@example.com")
        own_model, other_model = f"images-own-{suffix}", f"images-other-{suffix}"

        async def seed():
            result = {}
            async with db.session_factory() as session:
                for label, owner_id, model in (
                    ("own", own.id, own_model), ("other", other.id, other_model)
                ):
                    root, asset = await _seed_job(
                        session, user_id=owner_id, model_id=model,
                        created_at=BASE, prompt=f"{label} root", asset_dimensions=(512, 512),
                    )
                    child, _ = await _seed_job(
                        session, user_id=owner_id, model_id=model,
                        created_at=BASE + timedelta(seconds=1), prompt=f"{label} child",
                        source_asset_id=asset, asset_dimensions=(2048, 1024),
                    )
                    result[label] = (root, child)
                await session.commit()
            return result

        ids = client.portal.call(seed)

        async def cleanup():
            job_ids = [job_id for pair in ids.values() for job_id in pair]
            async with db.session_factory() as session:
                await session.execute(update(Job).where(Job.id.in_(job_ids)).values(source_asset_id=None))
                await session.execute(delete(Asset).where(Asset.job_id.in_(job_ids)))
                await session.execute(delete(Job).where(Job.id.in_(job_ids)))
                await session.execute(delete(Model).where(Model.id.in_([own_model, other_model])))
                await session.commit()

        try:
            assert _login(client, own_email).status_code == 204
            assert _ids(client.get("/api/v1/generations?size=2k")) == [str(ids["own"][1])]
            body = client.get("/api/v1/generations?size=2k&fields=ids").json()
            assert body["ids"] == [str(ids["own"][1])]
            roots = client.get("/api/v1/generations?size=2k&roots_only=true")
            assert _ids(roots) == [str(ids["own"][0])]
            assert client.get("/api/v1/generations", params={
                "size": "2k", "cursor": str(ids["other"][1]),
            }).status_code == 404
            assert client.get("/api/v1/generations", params={
                "size": "2k", "model_id": other_model,
            }).json() == []
            options = client.get("/api/v1/generations/models")
            assert options.status_code == 200
            assert [model["id"] for model in options.json()["models"]] == [own_model]

            async def disable():
                async with db.session_factory() as session:
                    await session.execute(update(User).where(User.id == own.id).values(state="disabled"))
                    await session.commit()

            client.portal.call(disable)
            assert client.get("/api/v1/generations?size=2k").status_code == 401
            assert client.get("/api/v1/generations/models").status_code == 401
        finally:
            client.portal.call(cleanup)
