from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select

from biodsh_platform.db import SessionLocal
from biodsh_platform.models import RefreshToken, User


def test_health_and_discovery_are_public(client: TestClient) -> None:
    assert client.get("/health").json()["database"] == "ok"
    assert client.get("/healthz/live").json()["database"] == "unchecked"
    discovery = client.get("/.well-known/biodsh-platform").json()
    assert discovery["api_versions"]["v1"] == "/api/v1"
    assert "project-rbac" in discovery["capabilities"]


def test_bootstrap_token_only_initializes_first_admin(
    client: TestClient, bootstrap_headers: dict[str, str]
) -> None:
    body = {"username": "Admin", "password": "correct-horse-battery", "is_admin": False}
    first = client.post("/api/v1/bootstrap/admin", headers=bootstrap_headers, json=body)
    assert first.status_code == 201
    assert first.json()["username"] == "admin"
    assert first.json()["is_admin"] is True
    assert (
        client.post("/api/v1/bootstrap/admin", headers=bootstrap_headers, json=body).status_code
        == 409
    )
    assert client.get("/api/v1/projects", headers=bootstrap_headers).status_code == 401


def test_login_refresh_rotation_logout_and_admin_revoke(
    client: TestClient, admin_session: dict, admin_headers: dict[str, str]
) -> None:
    assert client.get("/api/v1/auth/me", headers=admin_headers).json()["username"] == "admin"
    refreshed = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": admin_session["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": admin_session["refresh_token"]}
        ).status_code
        == 401
    )
    next_session = refreshed.json()
    assert (
        client.post(
            "/api/v1/auth/logout", json={"refresh_token": next_session["refresh_token"]}
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": next_session["refresh_token"]}
        ).status_code
        == 401
    )
    user_id = next_session["user"]["id"]
    assert (
        client.post(
            f"/api/v1/admin/users/{user_id}/revoke",
            headers={"Authorization": f"Bearer {next_session['access_token']}"},
        ).status_code
        == 204
    )
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {next_session['access_token']}"}
        ).status_code
        == 401
    )


def test_password_and_refresh_secrets_are_not_stored_verbatim(admin_session: dict) -> None:
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == "admin"))
        token = db.scalar(select(RefreshToken).where(RefreshToken.user_id == user.id))
        assert user.password_hash.startswith("$argon2id$")
        assert "correct-horse-battery" not in user.password_hash
        assert token.token_hash != admin_session["refresh_token"]
        assert len(token.token_hash) == 64


def test_invalid_login_is_audited(client: TestClient, admin_headers: dict[str, str]) -> None:
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "missing", "password": "incorrect-password"}
        ).status_code
        == 401
    )
    logs = client.get("/api/v1/audit-logs", headers=admin_headers).json()
    assert any(row["action"] == "auth.login" and row["outcome"] == "failure" for row in logs)


def test_admin_can_disable_user_and_security_changes_revoke_tokens(
    client: TestClient, user_factory: Callable, admin_headers: dict[str, str], admin_session: dict
) -> None:
    user, user_headers = user_factory("managed-user")
    updated = client.patch(
        f"/api/v1/admin/users/{user['id']}",
        headers=admin_headers,
        json={"is_active": False},
    )
    assert updated.status_code == 200
    assert updated.json()["is_active"] is False
    assert client.get("/api/v1/auth/me", headers=user_headers).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "managed-user", "password": "managed-user-secure-password"},
        ).status_code
        == 401
    )
    assert (
        client.patch(
            f"/api/v1/admin/users/{admin_session['user']['id']}",
            headers=admin_headers,
            json={"is_admin": False},
        ).status_code
        == 409
    )


def test_repeated_login_failures_lock_account(client: TestClient, user_factory: Callable) -> None:
    user_factory("locked-user")
    for _ in range(3):
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "locked-user", "password": "incorrect-password"},
        )
        assert response.status_code == 401
    correct = client.post(
        "/api/v1/auth/login",
        json={"username": "locked-user", "password": "locked-user-secure-password"},
    )
    assert correct.status_code == 401


def test_project_roles_and_cross_project_isolation(
    client: TestClient, user_factory: Callable, admin_headers: dict[str, str]
) -> None:
    owner, owner_headers = user_factory("owner")
    analyst, analyst_headers = user_factory("analyst")
    viewer, viewer_headers = user_factory("viewer")
    _, outsider_headers = user_factory("outsider")

    project = client.post(
        "/api/v1/projects", headers=owner_headers, json={"name": "Pilot", "description": "RBAC"}
    ).json()
    for user, role in ((analyst, "analyst"), (viewer, "viewer")):
        response = client.put(
            f"/api/v1/projects/{project['id']}/members/{user['id']}",
            headers=owner_headers,
            json={"role": role},
        )
        assert response.status_code == 200

    assert (
        client.get(f"/api/v1/projects/{project['id']}", headers=viewer_headers).status_code == 200
    )
    assert (
        client.get(f"/api/v1/projects/{project['id']}", headers=outsider_headers).status_code == 404
    )
    assert client.get("/api/v1/projects", headers=outsider_headers).json() == []
    assert (
        client.put(
            f"/api/v1/projects/{project['id']}/members/{owner['id']}",
            headers=viewer_headers,
            json={"role": "viewer"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/projects/{project['id']}/members/{viewer['id']}",
            headers=admin_headers,
            json={"role": "admin"},
        ).status_code
        == 422
    )

    job_body = {
        "project_id": project["id"],
        "kind": "scrna-inspect-qc",
        "payload": {"input": "dataset.h5ad"},
        "requested_resources": {"cpu": 2},
    }
    assert client.post("/api/v1/jobs", headers=viewer_headers, json=job_body).status_code == 403
    assert client.post("/api/v1/jobs", headers=outsider_headers, json=job_body).status_code == 404
    created = client.post("/api/v1/jobs", headers=analyst_headers, json=job_body)
    assert created.status_code == 201
    job = created.json()
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=viewer_headers).status_code == 200
    assert client.get(f"/api/v1/jobs/{job['id']}", headers=outsider_headers).status_code == 404
    assert (
        client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=viewer_headers).status_code == 403
    )
    assert (
        client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=analyst_headers).status_code == 200
    )
    assert (
        client.get(
            f"/api/v1/audit-logs?project_id={project['id']}", headers=viewer_headers
        ).status_code
        == 200
    )
    assert client.get("/api/v1/audit-logs", headers=viewer_headers).status_code == 403
    assert client.get("/api/v1/projects", headers=admin_headers).json()[0]["role"] == "admin"


def test_node_registration_uses_bootstrap_token_and_listing_requires_admin(
    client: TestClient,
    bootstrap_headers: dict[str, str],
    admin_headers: dict[str, str],
    user_factory: Callable,
) -> None:
    _, regular_headers = user_factory("regular")
    body = {"name": "node-01", "capabilities": {"cpu_total": 32}}
    first = client.post("/api/v1/nodes/register", headers=bootstrap_headers, json=body)
    second = client.post("/api/v1/nodes/register", headers=bootstrap_headers, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert (
        client.post("/api/v1/nodes/register", headers=regular_headers, json=body).status_code == 401
    )
    assert client.get("/api/v1/nodes", headers=regular_headers).status_code == 403
    assert client.get("/api/v1/nodes", headers=admin_headers).status_code == 200
