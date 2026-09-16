from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from biodsh_platform import feishu, notifications
from biodsh_platform.config import get_settings
from biodsh_platform.db import SessionLocal
from biodsh_platform.models import ExternalIdentity, Job, NotificationOutbox, ProjectChat, User
from biodsh_platform.services import append_job_event


@pytest.fixture
def enabled(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "feishu_login_enabled", True)
    monkeypatch.setattr(cfg, "feishu_app_id", "test-app")
    monkeypatch.setattr(
        cfg, "feishu_redirect_uri", "https://example.test/api/v1/auth/feishu/callback"
    )
    monkeypatch.setattr(cfg, "feishu_allowed_tenants", ["allowed"])
    monkeypatch.setattr(
        feishu,
        "identity_from_code",
        lambda code: dict(tenant_key="allowed", open_id="person", name="User"),
    )


def begin(client):
    flow = client.post("/api/v1/auth/feishu/start").json()
    state = parse_qs(urlparse(flow["authorization_url"]).query)["state"][0]
    return flow, state


def test_handoff_replay_and_local_permissions(client, enabled):
    flow, state = begin(client)
    assert (
        client.post("/api/v1/auth/feishu/poll", json=dict(poll_token=flow["poll_token"])).json()[
            "status"
        ]
        == "pending"
    )
    assert (
        client.get(
            "/api/v1/auth/feishu/callback", params=dict(code="code", state=state)
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/auth/feishu/callback", params=dict(code="code", state=state)
        ).status_code
        == 400
    )
    result = client.post("/api/v1/auth/feishu/poll", json=dict(poll_token=flow["poll_token"]))
    assert result.status_code == 200
    headers = {"Authorization": "Bearer " + result.json()["access_token"]}
    assert client.get("/api/v1/auth/me", headers=headers).json()["is_admin"] is False
    assert client.get("/api/v1/projects", headers=headers).json() == []
    assert (
        client.post(
            "/api/v1/auth/feishu/poll", json=dict(poll_token=flow["poll_token"])
        ).status_code
        == 400
    )
    with SessionLocal() as db:
        assert len(list(db.scalars(select(ExternalIdentity)))) == 1
    second, state2 = begin(client)
    assert (
        client.get(
            "/api/v1/auth/feishu/callback", params=dict(code="next", state=state2)
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        assert len(list(db.scalars(select(User)))) == 1


def test_wrong_tenant_state_and_disabled_account(client, enabled, monkeypatch):
    assert (
        client.get("/api/v1/auth/feishu/callback", params=dict(code="x", state="wrong")).status_code
        == 400
    )
    flow, state = begin(client)
    monkeypatch.setattr(
        feishu, "identity_from_code", lambda _: dict(tenant_key="outsider", open_id="person")
    )
    assert (
        client.get("/api/v1/auth/feishu/callback", params=dict(code="x", state=state)).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/auth/feishu/poll", json=dict(poll_token=flow["poll_token"])
        ).status_code
        == 400
    )
    monkeypatch.setattr(
        feishu, "identity_from_code", lambda _: dict(tenant_key="allowed", open_id="person")
    )
    flow, state = begin(client)
    assert (
        client.get("/api/v1/auth/feishu/callback", params=dict(code="x", state=state)).status_code
        == 200
    )
    with SessionLocal() as db:
        user = db.scalar(select(User))
        user.is_active = False
        db.commit()
    assert (
        client.post(
            "/api/v1/auth/feishu/poll", json=dict(poll_token=flow["poll_token"])
        ).status_code
        == 403
    )


def test_project_node_authorization(client, admin_headers, bootstrap_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "project_node_authorization_enabled", True)
    pid = client.post("/api/v1/projects", headers=admin_headers, json=dict(name="access")).json()[
        "id"
    ]
    nid = client.post(
        "/api/v1/nodes/register", headers=bootstrap_headers, json=dict(name="node")
    ).json()["node"]["id"]
    body = dict(project_id=pid, kind="test", node_ids=[nid])
    assert client.post("/api/v1/jobs", headers=admin_headers, json=body).status_code == 403
    assert (
        client.put(f"/api/v1/projects/{pid}/nodes", headers=admin_headers, json=[nid]).status_code
        == 200
    )
    assert client.post("/api/v1/jobs", headers=admin_headers, json=body).status_code == 201


def test_outbox_retry_and_dedup(client, admin_headers, monkeypatch):
    pid = client.post("/api/v1/projects", headers=admin_headers, json=dict(name="notify")).json()[
        "id"
    ]
    jobid = client.post(
        "/api/v1/jobs", headers=admin_headers, json=dict(project_id=pid, kind="test")
    ).json()["id"]
    with SessionLocal() as db:
        db.add(ProjectChat(project_id=pid, chat_id="oc_test"))
        db.commit()
        job = db.get(Job, jobid)
        append_job_event(db, job, kind="status", message="done", payload={"status": "succeeded"})
        db.commit()
        append_job_event(db, job, kind="status", message="done", payload={"status": "succeeded"})
        db.commit()
        assert len(list(db.scalars(select(NotificationOutbox)))) == 1
    monkeypatch.setattr(get_settings(), "feishu_notifications_enabled", True)
    monkeypatch.setattr(notifications, "tenant_token", lambda: "test")
    sent = []

    def fail(*args):
        raise RuntimeError("temporary provider outage")

    monkeypatch.setattr(notifications, "request_api", fail)
    notifications.deliver_once()
    with SessionLocal() as db:
        from datetime import UTC, datetime

        row = db.scalar(select(NotificationOutbox))
        assert row.status == "pending" and row.attempts == 1
        row.available_at = datetime.now(UTC)
        db.commit()
    monkeypatch.setattr(notifications, "request_api", lambda *args: sent.append(args))
    notifications.deliver_once()
    notifications.deliver_once()
    assert len(sent) == 1
    with SessionLocal() as db:
        assert db.scalar(select(NotificationOutbox)).status == "sent"


def test_migration_roundtrip_and_no_model_drift(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    monkeypatch.setattr(
        get_settings(), "database_url", "sqlite:///" + (tmp_path / "migration.db").as_posix()
    )
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.check(config)
