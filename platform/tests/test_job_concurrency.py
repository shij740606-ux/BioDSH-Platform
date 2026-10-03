"""Real concurrent transactions; PostgreSQL runs in a disposable private schema."""

import asyncio
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from biodsh_platform import api
from biodsh_platform.models import Base, ComputeNode, Job, JobArtifact, JobEvent, Project
from biodsh_platform.schemas import WorkerClaim, WorkerJobComplete, WorkerJobEventCreate
from biodsh_platform.services import append_job_event, request_job_cancellation


@pytest.fixture(params=["sqlite", "postgresql"])
def job_database(request, tmp_path):
    schema = None
    if request.param == "postgresql":
        url = os.environ.get("BIODSH_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("BIODSH_TEST_POSTGRES_URL not configured; PostgreSQL not verified")
        root = create_engine(url)
        schema = "biodsh_test_" + uuid.uuid4().hex
        with root.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = root.execution_options(schema_translate_map={None: schema})
    else:
        root = engine = create_engine(
            f"sqlite:///{tmp_path / 'concurrency.db'}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        db.add(Project(id="project", name="Concurrency"))
        db.add(
            ComputeNode(
                id="node",
                name="test-node",
                status="online",
                last_heartbeat_at=datetime.now(UTC),
                capabilities={"executors": ["test"]},
            )
        )
        db.commit()
    try:
        yield sessions
    finally:
        if schema:
            with root.begin() as connection:
                connection.execute(DropSchema(schema, cascade=True))
        root.dispose()


def seed(sessions, status="running", *, expired=False):
    with sessions() as db:
        db.add(
            Job(
                id="job",
                project_id="project",
                node_id="node",
                kind="test",
                status=status,
                attempt=1,
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=-1 if expired else 45),
            )
        )
        db.commit()


def act(db, action, job, node):
    if action == "cancel":
        request_job_cancellation(db, job)
        db.commit()
    elif action == "start":
        api.start_job(node.id, job.id, db, node)
    elif action == "complete":
        api.complete_job(
            node.id, job.id, WorkerJobComplete(status="succeeded", exit_code=0), db, node
        )
    elif action == "control":
        api.worker_job_control(node.id, job.id, db, node)
    elif action == "log":
        api.create_worker_job_event(
            node.id, job.id, WorkerJobEventCreate(kind="log", message="concurrent"), db, node
        )


@pytest.mark.parametrize(
    ("initial", "actions", "allowed"),
    [
        ("running", ("cancel", "complete"), {"succeeded", "cancelled"}),
        ("assigned", ("cancel", "start"), {"cancelling"}),
        ("running", ("complete", "control"), {"succeeded"}),
        ("running", ("cancel", "control"), {"cancelling"}),
        ("running", ("cancel", "log"), {"cancelling"}),
        ("running", ("complete", "log"), {"succeeded"}),
    ],
)
def test_concurrent_transitions_do_not_overwrite_terminal_state(
    job_database, initial, actions, allowed
):
    seed(job_database, initial)
    barrier = threading.Barrier(2)

    def run(action):
        with job_database() as db:
            job, node = db.get(Job, "job"), db.get(ComputeNode, "node")
            barrier.wait(timeout=5)  # both sessions retain the same pre-transition snapshot
            try:
                act(db, action, job, node)
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, actions))
    assert all(code in (200, 409) for code in results), results
    with job_database() as db:
        job = db.get(Job, "job")
        assert job.status in allowed
        assert (job.lease_expires_at is None) == (job.status in {"succeeded", "cancelled"})
        if job.status in {"succeeded", "cancelled"}:
            assert job.finished_at is not None
        sequences = list(db.scalars(select(JobEvent.sequence).order_by(JobEvent.sequence)))
        assert sequences == list(range(1, len(sequences) + 1))


def test_events_are_unique_across_transactions_and_repeated_appends(job_database):
    seed(job_database)
    barrier = threading.Barrier(6)

    def run(index):
        with job_database() as db:
            job = db.get(Job, "job")
            barrier.wait(timeout=5)
            for _ in range(3):
                append_job_event(db, job, kind="log", message=str(index))
            db.commit()

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(run, range(6)))
    with job_database() as db:
        sequences = list(db.scalars(select(JobEvent.sequence).order_by(JobEvent.sequence)))
    assert sequences == list(range(1, 19))


@pytest.mark.parametrize("winner", ["control", "complete"])
def test_expiry_rechecks_renewed_lease_after_stale_scan(job_database, monkeypatch, winner):
    seed(job_database, expired=True)
    scanned, renewed = threading.Event(), threading.Event()
    original = api.lock_job

    def lock(db, job_id):
        if threading.current_thread().name == "claimer":
            scanned.set()
            assert renewed.wait(5)
        return original(db, job_id)

    monkeypatch.setattr(api, "lock_job", lock)
    # SQLite's claim already holds its database-wide write lock, so another
    # writer cannot renew between scan and row lock. Test the PostgreSQL race.
    if job_database.kw["bind"].dialect.name == "sqlite":
        pytest.skip("SQLite serializes all writers before the expiry scan")

    def claim():
        threading.current_thread().name = "claimer"
        with job_database() as db:
            return api.claim_job(
                "node", WorkerClaim(supported_kinds=["test"]), db, db.get(ComputeNode, "node")
            )

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(claim)
        assert scanned.wait(5)
        try:
            with job_database() as db:
                act(db, winner, db.get(Job, "job"), db.get(ComputeNode, "node"))
        finally:
            renewed.set()
        assert future.result(timeout=5) is None
    with job_database() as db:
        assert db.get(Job, "job").status == ("running" if winner == "control" else "succeeded")


@pytest.mark.parametrize("finish_during_upload", [False, True])
def test_upload_releases_transaction_then_revalidates_job(
    job_database, monkeypatch, tmp_path, finish_during_upload
):
    seed(job_database)
    settings = api.get_settings().model_copy(update={"artifact_root": tmp_path / "artifacts"})
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    renewed = threading.Event()

    def concurrent_control():
        with job_database() as db:
            api.worker_job_control("node", "job", db, db.get(ComputeNode, "node"))
        renewed.set()
        if finish_during_upload:
            with job_database() as db:
                act(db, "complete", db.get(Job, "job"), db.get(ComputeNode, "node"))

    class Request:
        headers = {"content-length": "6", "content-type": "text/plain"}

        async def stream(self):
            yield b"abc"
            # A real independent DB transaction must commit while the upload
            # body is still being consumed; a lock held across stream deadlocks.
            await asyncio.wait_for(asyncio.to_thread(concurrent_control), timeout=5)
            yield b"def"

    with job_database() as db:
        if finish_during_upload:
            with pytest.raises(HTTPException) as rejected:
                asyncio.run(
                    api.upload_job_artifact(
                        "node", "job", "result.txt", Request(), db, db.get(ComputeNode, "node")
                    )
                )
            assert rejected.value.status_code == 409
            db.rollback()
        else:
            artifact = asyncio.run(
                api.upload_job_artifact(
                    "node", "job", "result.txt", Request(), db, db.get(ComputeNode, "node")
                )
            )
            assert artifact.size_bytes == 6
    assert renewed.is_set()
    with job_database() as db:
        artifacts = list(db.scalars(select(JobArtifact)))
        assert len(artifacts) == (0 if finish_during_upload else 1)
    if finish_during_upload:
        assert not list(settings.artifact_root.rglob("*.part"))


def test_terminal_job_rejects_stale_cancel_and_control(job_database):
    seed(job_database)
    with job_database() as stale, job_database() as winner:
        old = stale.get(Job, "job")
        act(winner, "complete", winner.get(Job, "job"), winner.get(ComputeNode, "node"))
        with pytest.raises(HTTPException) as rejected:
            request_job_cancellation(stale, old)
        assert rejected.value.status_code == 409
        stale.rollback()
        with pytest.raises(HTTPException):
            api.worker_job_control("node", "job", stale, stale.get(ComputeNode, "node"))
    with job_database() as db:
        job = db.get(Job, "job")
        assert job.status == "succeeded" and job.lease_expires_at is None
