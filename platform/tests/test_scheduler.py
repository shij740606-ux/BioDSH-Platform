from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from biodsh_platform.db import SessionLocal
from biodsh_platform.models import Job, JobStatus


def node(client, bootstrap_headers, name, cpu_percent=10, vram=16000):
    enrolled = client.post(
        "/api/v1/nodes/register",
        headers=bootstrap_headers,
        json=dict(name=name, hostname=name, operating_system="Linux", capabilities={}),
    ).json()
    nid = enrolled["node"]["id"]
    headers = {"X-BioDSH-Node-ID": nid, "X-BioDSH-Node-Token": enrolled["node_token"]}
    result = client.post(
        f"/api/v1/nodes/{nid}/heartbeat",
        headers=headers,
        json=dict(
            capabilities=dict(cpu_logical=16, executors=["test"]),
            metric=dict(
                cpu_percent=cpu_percent,
                memory_used_bytes=1024,
                memory_total_bytes=100000,
                memory_percent=1,
                disk_used_bytes=100,
                disk_total_bytes=100000,
                disk_percent=1,
                network_rx_bytes=0,
                network_tx_bytes=0,
                running_jobs=0,
                gpus=[dict(index=2, memory_used_mib=0, memory_total_mib=vram)],
            ),
        ),
    )
    assert result.status_code == 200
    return nid, headers


def project(client, headers):
    return client.post("/api/v1/projects", headers=headers, json=dict(name="schedule")).json()["id"]


def create(client, headers, pid, scope=None, **resources):
    result = client.post(
        "/api/v1/jobs",
        headers=headers,
        json=dict(project_id=pid, kind="test", node_ids=scope, requested_resources=resources),
    )
    assert result.status_code == 201, result.text
    return result.json()


def claim(client, nid, headers):
    result = client.post(
        f"/api/v1/nodes/{nid}/jobs/claim", headers=headers, json=dict(supported_kinds=["test"])
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_lowest_load_scope_and_gpu_allocation(client, bootstrap_headers, admin_headers):
    pid = project(client, admin_headers)
    slow, sh = node(client, bootstrap_headers, "slow", 70)
    fast, fh = node(client, bootstrap_headers, "fast", 10)
    first = create(client, admin_headers, pid, cpu=2, gpu_count=1, gpu_memory_mib=12000)
    assert claim(client, slow, sh) is None
    assigned = claim(client, fast, fh)
    assert assigned["id"] == first["id"]
    assert assigned["payload"]["_allocation"]["gpu_indices"] == ["2"]
    scoped = create(client, admin_headers, pid, [slow], cpu=2)
    assert claim(client, fast, fh) is None
    assert claim(client, slow, sh)["id"] == scoped["id"]


def test_atomic_reservation_and_release(client, bootstrap_headers, admin_headers):
    pid = project(client, admin_headers)
    nid, nh = node(client, bootstrap_headers, "one")
    create(client, admin_headers, pid, cpu=2)
    create(client, admin_headers, pid, cpu=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: claim(client, nid, nh), range(2)))
    assert sum(v is not None for v in values) == 1
    assigned = next(v for v in values if v)
    assert (
        client.post(
            f"/api/v1/nodes/{nid}/jobs/{assigned['id']}/start", headers=nh, json={}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/nodes/{nid}/jobs/{assigned['id']}/complete",
            headers=nh,
            json=dict(status="succeeded", exit_code=0, result_manifest={}),
        ).status_code
        == 200
    )
    assert claim(client, nid, nh) is not None


def test_queue_reason_unknown_reservation_and_scope_validation(
    client, bootstrap_headers, admin_headers
):
    pid = project(client, admin_headers)
    nid, nh = node(client, bootstrap_headers, "small-gpu", vram=1000)
    job = create(client, admin_headers, pid, [nid], gpu_count=1, gpu_memory_mib=2000)
    assert claim(client, nid, nh) is None
    report = client.get(f"/api/v1/jobs/{job['id']}/scheduling", headers=admin_headers).json()
    assert report["selected_node_id"] is None
    assert "不足" in report["nodes"][0]["reason"]
    assert (
        client.post(
            "/api/v1/jobs",
            headers=admin_headers,
            json=dict(project_id=pid, kind="test", node_ids=[]),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/jobs",
            headers=admin_headers,
            json=dict(project_id=pid, kind="test", node_ids=["missing"]),
        ).status_code
        == 422
    )
    runnable = create(client, admin_headers, pid, [nid], cpu=2)
    assert claim(client, nid, nh)["id"] == runnable["id"]
    with SessionLocal() as db:
        row = db.get(Job, runnable["id"])
        row.status = JobStatus.RUNNING
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=60)
        db.commit()
    create(client, admin_headers, pid, [nid], cpu=1)
    assert claim(client, nid, nh) is None
    with SessionLocal() as db:
        assert db.get(Job, runnable["id"]).status == JobStatus.UNKNOWN


def test_project_permission_on_scheduler(client, bootstrap_headers, user_factory):
    _, owner = user_factory("scheduler-owner")
    _, other = user_factory("scheduler-outsider")
    pid = project(client, owner)
    nid, _ = node(client, bootstrap_headers, "private-task")
    job = create(client, owner, pid, [nid], cpu=1)
    assert client.get(f"/api/v1/jobs/{job['id']}/scheduling", headers=other).status_code == 404
