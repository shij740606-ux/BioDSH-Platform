from fastapi.testclient import TestClient


def test_health_is_public(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_api_requires_token(client: TestClient) -> None:
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/status").status_code == 401


def test_authenticated_platform_status(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.get("/api/status", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["service"] == "biodsh-platform"


def test_project_job_and_cancellation_flow(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project_response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "Single-cell pilot", "description": "Foundation test"},
    )
    assert project_response.status_code == 201
    project = project_response.json()

    job_response = client.post(
        "/api/jobs",
        headers=auth_headers,
        json={
            "project_id": project["id"],
            "kind": "scrna-inspect-qc",
            "payload": {"input": "dataset.h5ad"},
            "requested_resources": {"cpu": 2, "memory_gb": 4},
        },
    )
    assert job_response.status_code == 201
    job = job_response.json()
    assert job["status"] == "queued"

    events = client.get(f"/api/jobs/{job['id']}/events", headers=auth_headers)
    assert events.status_code == 200
    assert events.json()[0]["payload"]["status"] == "queued"

    cancelled = client.post(f"/api/jobs/{job['id']}/cancel", headers=auth_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{job['id']}/cancel", headers=auth_headers).status_code == 409


def test_node_registration_is_idempotent(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    body = {"name": "node-01", "capabilities": {"cpu_total": 32}}
    first = client.post("/api/nodes/register", headers=auth_headers, json=body)
    second = client.post("/api/nodes/register", headers=auth_headers, json=body)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["status"] == "online"
