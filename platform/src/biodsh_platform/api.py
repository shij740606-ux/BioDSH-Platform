from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from biodsh_platform import __version__
from biodsh_platform.db import get_db
from biodsh_platform.models import ComputeNode, Job, JobEvent, JobStatus, NodeStatus, Project
from biodsh_platform.schemas import (
    HealthRead,
    JobCreate,
    JobEventRead,
    JobRead,
    NodeHeartbeat,
    NodeRead,
    NodeRegister,
    ProjectCreate,
    ProjectRead,
)
from biodsh_platform.security import PlatformActor, require_platform_token
from biodsh_platform.services import append_job_event, get_job_or_404, request_job_cancellation

DbSession = Annotated[Session, Depends(get_db)]
health_router = APIRouter(tags=["health"])
api_router = APIRouter(prefix="/api", dependencies=[Depends(require_platform_token)])


@health_router.get("/health", response_model=HealthRead)
def health(db: DbSession) -> HealthRead:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"
    return HealthRead(
        status="ok" if database == "ok" else "degraded",
        service="biodsh-platform",
        version=__version__,
        database=database,
    )


@api_router.get("/status", response_model=HealthRead)
def authenticated_status(db: DbSession) -> HealthRead:
    return health(db)


@api_router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, db: DbSession, actor: PlatformActor) -> Project:
    project = Project(name=body.name, description=body.description, created_by=actor)
    db.add(project)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name already exists") from exc
    db.refresh(project)
    return project


@api_router.get("/projects", response_model=list[ProjectRead])
def list_projects(db: DbSession) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.created_at.desc())))


@api_router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, db: DbSession) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@api_router.post("/nodes/register", response_model=NodeRead, status_code=status.HTTP_201_CREATED)
def register_node(body: NodeRegister, db: DbSession) -> ComputeNode:
    now = datetime.now(UTC)
    node = db.scalar(select(ComputeNode).where(ComputeNode.name == body.name))
    if node is None:
        node = ComputeNode(name=body.name)
        db.add(node)
    node.status = NodeStatus.ONLINE
    node.capabilities = body.capabilities
    node.last_heartbeat_at = now
    db.commit()
    db.refresh(node)
    return node


@api_router.post("/nodes/{node_id}/heartbeat", response_model=NodeRead)
def heartbeat_node(node_id: str, body: NodeHeartbeat, db: DbSession) -> ComputeNode:
    node = db.get(ComputeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Compute node not found")
    node.status = NodeStatus.ONLINE
    node.capabilities = body.capabilities
    node.last_heartbeat_at = datetime.now(UTC)
    db.commit()
    db.refresh(node)
    return node


@api_router.get("/nodes", response_model=list[NodeRead])
def list_nodes(db: DbSession) -> list[ComputeNode]:
    return list(db.scalars(select(ComputeNode).order_by(ComputeNode.name)))


@api_router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(body: JobCreate, db: DbSession, actor: PlatformActor) -> Job:
    if db.get(Project, body.project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    job = Job(
        project_id=body.project_id,
        kind=body.kind,
        payload=body.payload,
        requested_resources=body.requested_resources,
        created_by=actor,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    db.flush()
    append_job_event(
        db,
        job,
        kind="status",
        message="Job queued",
        payload={"status": JobStatus.QUEUED},
    )
    db.commit()
    db.refresh(job)
    return job


@api_router.get("/jobs", response_model=list[JobRead])
def list_jobs(db: DbSession, project_id: str | None = None) -> list[Job]:
    query = select(Job).order_by(Job.created_at.desc())
    if project_id is not None:
        query = query.where(Job.project_id == project_id)
    return list(db.scalars(query))


@api_router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: str, db: DbSession) -> Job:
    return get_job_or_404(db, job_id)


@api_router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: str, db: DbSession) -> Job:
    job = request_job_cancellation(db, get_job_or_404(db, job_id))
    db.commit()
    db.refresh(job)
    return job


@api_router.get("/jobs/{job_id}/events", response_model=list[JobEventRead])
def list_job_events(job_id: str, db: DbSession) -> list[JobEvent]:
    get_job_or_404(db, job_id)
    return list(
        db.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.sequence)
        )
    )
