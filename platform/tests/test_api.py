from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select

from biodsh_platform.db import SessionLocal
from biodsh_platform.models import ComputeNode, NodeMetric, RefreshToken, User


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
    body = {
        "name": " node-01 ",
        "hostname": "compute-01",
        "operating_system": "Linux",
        "agent_version": "0.1.0",
        "capabilities": {"cpu_logical": 32},
    }
    first = client.post("/api/v1/nodes/register", headers=bootstrap_headers, json=body)
    second = client.post("/api/v1/nodes/register", headers=bootstrap_headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 409
    enrollment = first.json()
    node = enrollment["node"]
    assert node["name"] == "node-01"
    assert (
        client.post(
            "/api/v1/nodes/register",
            headers=bootstrap_headers,
            json={**body, "name": "   "},
        ).status_code
        == 422
    )
    node_headers = {
        "X-BioDSH-Node-ID": node["id"],
        "X-BioDSH-Node-Token": enrollment["node_token"],
    }
    metric = {
        "cpu_percent": 42.5,
        "load_one": 3.1,
        "memory_used_bytes": 8_000_000_000,
        "memory_total_bytes": 16_000_000_000,
        "memory_percent": 50,
        "disk_used_bytes": 200_000_000_000,
        "disk_total_bytes": 1_000_000_000_000,
        "disk_percent": 20,
        "network_rx_bytes": 1234,
        "network_tx_bytes": 5678,
        "running_jobs": 1,
        "gpus": [{"index": 0, "name": "Test GPU", "utilization_percent": 75}],
    }
    heartbeat = client.post(
        f"/api/v1/nodes/{node['id']}/heartbeat",
        headers=node_headers,
        json={"capabilities": body["capabilities"], "metric": metric},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["latest_metric"]["cpu_percent"] == 42.5
    assert (
        client.post(
            f"/api/v1/nodes/{node['id']}/heartbeat",
            headers=bootstrap_headers,
            json={"capabilities": {}, "metric": metric},
        ).status_code
        == 401
    )
    assert (
        client.post("/api/v1/nodes/register", headers=regular_headers, json=body).status_code == 401
    )
    assert client.get("/api/v1/nodes", headers=regular_headers).status_code == 403
    listed = client.get("/api/v1/nodes", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["latest_metric"]["memory_percent"] == 50
    history = client.get(f"/api/v1/nodes/{node['id']}/metrics", headers=admin_headers)
    assert history.status_code == 200
    assert len(history.json()) == 1
    assert (
        client.patch(
            f"/api/v1/nodes/{node['id']}",
            headers=admin_headers,
            json={"status": "disabled"},
        ).json()["status"]
        == "disabled"
    )
    rotated = client.post(f"/api/v1/nodes/{node['id']}/rotate-token", headers=admin_headers).json()
    assert rotated["node_token"] != enrollment["node_token"]
    assert (
        client.post(
            f"/api/v1/nodes/{node['id']}/heartbeat",
            headers=node_headers,
            json={"capabilities": {}, "metric": metric},
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        stored_node = db.get(ComputeNode, node["id"])
        assert stored_node.token_hash != rotated["node_token"]
        assert db.scalar(select(NodeMetric).where(NodeMetric.node_id == node["id"])) is not None


def test_worker_claim_resource_matching_logs_cancel_and_artifacts(
    client: TestClient,
    bootstrap_headers: dict[str, str],
    user_factory: Callable,
) -> None:
    _, owner_headers = user_factory("job-owner")
    _, outsider_headers = user_factory("job-outsider")
    project = client.post(
        "/api/v1/projects",
        headers=owner_headers,
        json={"name": "Execution", "description": "worker lifecycle"},
    ).json()
    impossible = client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "project_id": project["id"],
            "kind": "test-command",
            "payload": {"arguments": ["--large"]},
            "requested_resources": {"gpu_count": 2},
            "priority": 100,
        },
    )
    assert impossible.status_code == 201
    runnable = client.post(
        "/api/v1/jobs",
        headers=owner_headers,
        json={
            "project_id": project["id"],
            "kind": "test-command",
            "payload": {"arguments": ["--input", "sample.txt"]},
            "requested_resources": {"cpu": 2, "memory_bytes": 1024},
            "priority": 5,
        },
    )
    assert runnable.status_code == 201
    enrollment = client.post(
        "/api/v1/nodes/register",
        headers=bootstrap_headers,
        json={
            "name": "executor-01",
            "hostname": "executor-01",
            "operating_system": "Linux",
            "agent_version": "0.1.0",
            "capabilities": {"cpu_logical": 4},
        },
    ).json()
    node = enrollment["node"]
    node_headers = {
        "X-BioDSH-Node-ID": node["id"],
        "X-BioDSH-Node-Token": enrollment["node_token"],
    }
    heartbeat = {
        "capabilities": {"cpu_logical": 4, "executors": ["test-command"]},
        "metric": {
            "cpu_percent": 10,
            "load_one": 0.5,
            "memory_used_bytes": 1000,
            "memory_total_bytes": 10000,
            "memory_percent": 10,
            "disk_used_bytes": 1000,
            "disk_total_bytes": 10000,
            "disk_percent": 10,
            "network_rx_bytes": 1,
            "network_tx_bytes": 2,
            "running_jobs": 0,
            "gpus": [{"index": 0, "memory_used_mib": 0, "memory_total_mib": 1000}],
        },
    }
    assert (
        client.post(
            f"/api/v1/nodes/{node['id']}/heartbeat", headers=node_headers, json=heartbeat
        ).status_code
        == 200
    )
    assert client.get("/api/v1/job-kinds", headers=owner_headers).json() == ["test-command"]
    assert (
        client.post(
            f"/api/v1/nodes/{node['id']}/jobs/claim",
            headers=node_headers,
            json={"supported_kinds": ["unadvertised-command"]},
        ).status_code
        == 422
    )
    claimed = client.post(
        f"/api/v1/nodes/{node['id']}/jobs/claim",
        headers=node_headers,
        json={"supported_kinds": ["test-command"]},
    )
    assert claimed.status_code == 200
    job = claimed.json()
    assert job["id"] == runnable.json()["id"]
    assert job["status"] == "assigned"
    assert job["attempt"] == 1
    assert (
        client.post(
            f"/api/v1/nodes/not-this-node/jobs/{job['id']}/start", headers=node_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/nodes/{node['id']}/jobs/{job['id']}/start", headers=node_headers
        ).json()["status"]
        == "running"
    )
    event = client.post(
        f"/api/v1/nodes/{node['id']}/jobs/{job['id']}/events",
        headers=node_headers,
        json={"kind": "log", "message": "processing sample", "payload": {}},
    )
    assert event.status_code == 200
    assert event.json()["message"] == "processing sample"
    control_url = f"/api/v1/nodes/{node['id']}/jobs/{job['id']}/control"
    assert client.post(control_url, headers=node_headers).json()["cancel_requested"] is False
    assert (
        client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=owner_headers).json()["status"]
        == "cancelling"
    )
    assert client.post(control_url, headers=node_headers).json()["cancel_requested"] is True
    artifact = client.put(
        f"/api/v1/nodes/{node['id']}/jobs/{job['id']}/artifacts/results/summary.txt",
        headers={**node_headers, "Content-Type": "text/plain"},
        content=b"completed output\n",
    )
    assert artifact.status_code == 200
    completed = client.post(
        f"/api/v1/nodes/{node['id']}/jobs/{job['id']}/complete",
        headers=node_headers,
        json={
            "status": "succeeded",
            "exit_code": -15,
            "error_message": "process exited with code -15",
            "result_manifest": {"artifacts": [artifact.json()]},
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "cancelled"
    artifacts = client.get(f"/api/v1/jobs/{job['id']}/artifacts", headers=owner_headers).json()
    assert artifacts[0]["sha256"] == artifact.json()["sha256"]
    download = client.get(
        f"/api/v1/jobs/{job['id']}/artifacts/{artifacts[0]['id']}", headers=owner_headers
    )
    assert download.content == b"completed output\n"
    assert (
        client.get(f"/api/v1/jobs/{job['id']}/artifacts", headers=outsider_headers).status_code
        == 404
    )
    no_match = client.post(
        f"/api/v1/nodes/{node['id']}/jobs/claim",
        headers=node_headers,
        json={"supported_kinds": ["test-command"]},
    )
    assert no_match.status_code == 200
    assert no_match.json() is None
