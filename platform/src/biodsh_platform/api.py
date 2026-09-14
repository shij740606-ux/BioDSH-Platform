import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from biodsh_platform import __version__
from biodsh_platform.audit import audit
from biodsh_platform.authorization import accessible_project_ids, project_role, require_project
from biodsh_platform.config import get_settings
from biodsh_platform.db import get_db
from biodsh_platform.models import (
    AuditLog,
    ComputeNode,
    Job,
    JobArtifact,
    JobEvent,
    JobStatus,
    NodeMetric,
    NodeStatus,
    Project,
    ProjectMember,
    ProjectRole,
    RefreshToken,
    User,
    new_id,
)
from biodsh_platform.schemas import (
    AuditLogRead,
    HealthRead,
    JobArtifactRead,
    JobCreate,
    JobEventRead,
    JobRead,
    LoginRequest,
    MemberSet,
    NodeEnrollment,
    NodeHeartbeat,
    NodeMetricRead,
    NodeRead,
    NodeRegister,
    NodeUpdate,
    PlatformMetadata,
    ProjectCreate,
    ProjectMemberRead,
    ProjectRead,
    RefreshRequest,
    TokenRead,
    UserCreate,
    UserRead,
    UserUpdate,
    WorkerClaim,
    WorkerJobComplete,
    WorkerJobControl,
    WorkerJobEventCreate,
)
from biodsh_platform.security import (
    AdminUser,
    CurrentUser,
    NodeActor,
    PlatformActor,
    create_access_token,
    find_refresh_token,
    hash_opaque_token,
    hash_password,
    issue_node_token,
    issue_refresh_token,
    normalize_username,
    password_hasher,
    verify_password,
)
from biodsh_platform.services import (
    append_job_event,
    get_job_or_404,
    job_fits_node,
    request_job_cancellation,
)

DbSession = Annotated[Session, Depends(get_db)]
health_router = APIRouter(tags=["health"])
api_router = APIRouter()


def token_response(db: Session, user: User) -> tuple[TokenRead, RefreshToken]:
    access_token, expires_in = create_access_token(user)
    refresh_token, record = issue_refresh_token(db, user)
    return (
        TokenRead(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            user=user,
        ),
        record,
    )


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def assigned_job(db: Session, node: ComputeNode, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None or job.node_id != node.id:
        raise HTTPException(status_code=404, detail="Assigned job not found")
    return job


def artifact_relative_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise HTTPException(status_code=422, detail="Invalid artifact path")
    normalized = str(path)
    if len(normalized) > 500:
        raise HTTPException(status_code=422, detail="Artifact path is too long")
    return normalized


def project_response(db: Session, user: User, project: Project) -> ProjectRead:
    return ProjectRead.model_validate(project).model_copy(
        update={"role": project_role(db, user, project.id)}
    )


def node_response(db: Session, node: ComputeNode) -> NodeRead:
    latest = db.scalar(
        select(NodeMetric)
        .where(NodeMetric.node_id == node.id)
        .order_by(NodeMetric.collected_at.desc())
        .limit(1)
    )
    effective_status = node.status
    if node.status == NodeStatus.ONLINE:
        cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().node_offline_seconds)
        if node.last_heartbeat_at is None or aware(node.last_heartbeat_at) < cutoff:
            effective_status = NodeStatus.OFFLINE
    return NodeRead.model_validate(node).model_copy(
        update={
            "status": effective_status,
            "latest_metric": NodeMetricRead.model_validate(latest) if latest else None,
        }
    )


@health_router.get("/health", response_model=HealthRead)
def health(db: DbSession) -> HealthRead:
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"
    return HealthRead(
        status="ok" if database == "ok" else "degraded",
        service=get_settings().service_name,
        version=__version__,
        database=database,
    )


@health_router.get("/healthz/live", response_model=HealthRead)
def liveness(db: DbSession) -> HealthRead:
    del db
    return HealthRead(
        status="ok", service=get_settings().service_name, version=__version__, database="unchecked"
    )


@health_router.get("/healthz/ready", response_model=HealthRead)
def readiness(db: DbSession) -> HealthRead:
    return health(db)


@health_router.get("/.well-known/biodsh-platform", response_model=PlatformMetadata)
def platform_metadata() -> PlatformMetadata:
    settings = get_settings()
    return PlatformMetadata(
        service=settings.service_name,
        version=__version__,
        api_versions={"v1": settings.api_prefix},
        health={"liveness": "/healthz/live", "readiness": "/healthz/ready"},
        capabilities=[
            "password-auth",
            "refresh-token-rotation",
            "project-rbac",
            "audit-log",
            "multi-node",
            "node-resource-metrics",
            "leased-job-execution",
            "live-job-events",
            "job-artifacts",
        ],
    )


@api_router.post(
    "/bootstrap/admin",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    tags=["bootstrap"],
)
def bootstrap_admin(body: UserCreate, db: DbSession, _: PlatformActor) -> User:
    if db.scalar(select(User.id).where(User.is_admin.is_(True)).limit(1)) is not None:
        raise HTTPException(status_code=409, detail="An administrator already exists")
    user = User(
        username=normalize_username(body.username),
        password_hash=hash_password(body.password),
        is_admin=True,
    )
    db.add(user)
    try:
        db.flush()
        audit(
            db, "admin.bootstrap", actor_user_id=user.id, resource_type="user", resource_id=user.id
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    db.refresh(user)
    return user


@api_router.post("/auth/login", response_model=TokenRead, tags=["auth"])
def login(body: LoginRequest, request: Request, db: DbSession) -> TokenRead:
    username = normalize_username(body.username)
    user = db.scalar(select(User).where(User.username == username))
    now = datetime.now(UTC)
    locked = user is not None and user.locked_until is not None and aware(user.locked_until) > now
    valid_password = verify_password(
        user.password_hash if user is not None and not locked else None, body.password
    )
    if user is None or not user.is_active or locked or not valid_password:
        if user is not None and user.is_active and not locked:
            user.failed_login_count += 1
            settings = get_settings()
            if user.failed_login_count >= settings.login_max_attempts:
                user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
                user.failed_login_count = 0
        audit(
            db,
            "auth.login",
            outcome="failure",
            details={
                "username": username,
                "client": request.client.host if request.client else None,
            },
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    response, _ = token_response(db, user)
    audit(
        db,
        "auth.login",
        actor_user_id=user.id,
        details={"client": request.client.host if request.client else None},
    )
    db.commit()
    return response


@api_router.post("/auth/refresh", response_model=TokenRead, tags=["auth"])
def refresh(body: RefreshRequest, db: DbSession) -> TokenRead:
    record = find_refresh_token(db, body.refresh_token)
    now = datetime.now(UTC)
    if record is None or record.revoked_at is not None or aware(record.expires_at) <= now:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user = db.get(User, record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    record.revoked_at = now
    record.last_used_at = now
    response, replacement = token_response(db, user)
    record.replaced_by_id = replacement.id
    audit(db, "auth.refresh", actor_user_id=user.id)
    db.commit()
    return response


@api_router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
def logout(body: RefreshRequest, db: DbSession) -> None:
    record = find_refresh_token(db, body.refresh_token)
    if record and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        audit(db, "auth.logout", actor_user_id=record.user_id)
        db.commit()


@api_router.get("/auth/me", response_model=UserRead, tags=["auth"])
def me(user: CurrentUser) -> User:
    return user


@api_router.get("/status", response_model=HealthRead)
def authenticated_status(db: DbSession, _: CurrentUser) -> HealthRead:
    return health(db)


@api_router.post(
    "/admin/users", response_model=UserRead, status_code=status.HTTP_201_CREATED, tags=["admin"]
)
def create_user(body: UserCreate, db: DbSession, admin: AdminUser) -> User:
    user = User(
        username=normalize_username(body.username),
        password_hash=hash_password(body.password),
        is_admin=body.is_admin,
    )
    db.add(user)
    try:
        db.flush()
        audit(
            db,
            "user.create",
            actor_user_id=admin.id,
            resource_type="user",
            resource_id=user.id,
            details={"is_admin": user.is_admin},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    db.refresh(user)
    return user


@api_router.get("/admin/users", response_model=list[UserRead], tags=["admin"])
def list_users(db: DbSession, _: AdminUser) -> list[User]:
    return list(db.scalars(select(User).order_by(User.username)))


def revoke_tokens(db: Session, user: User) -> None:
    user.token_version += 1
    now = datetime.now(UTC)
    for token in db.scalars(
        select(RefreshToken).where(
            RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
        )
    ):
        token.revoked_at = now


@api_router.patch("/admin/users/{user_id}", response_model=UserRead, tags=["admin"])
def update_user(user_id: str, body: UserUpdate, db: DbSession, admin: AdminUser) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    next_active = user.is_active if body.is_active is None else body.is_active
    next_admin = user.is_admin if body.is_admin is None else body.is_admin
    if user.is_admin and (not next_active or not next_admin):
        other_admin = db.scalar(
            select(User.id)
            .where(User.id != user.id, User.is_admin.is_(True), User.is_active.is_(True))
            .limit(1)
        )
        if other_admin is None:
            raise HTTPException(
                status_code=409, detail="The system must keep an active administrator"
            )
    security_changed = user.is_active != next_active or user.is_admin != next_admin
    user.is_active = next_active
    user.is_admin = next_admin
    if security_changed:
        revoke_tokens(db, user)
    audit(
        db,
        "user.update",
        actor_user_id=admin.id,
        resource_type="user",
        resource_id=user.id,
        details={"is_active": user.is_active, "is_admin": user.is_admin},
    )
    db.commit()
    db.refresh(user)
    return user


@api_router.post(
    "/admin/users/{user_id}/revoke", status_code=status.HTTP_204_NO_CONTENT, tags=["admin"]
)
def revoke_user_tokens(user_id: str, db: DbSession, admin: AdminUser) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    revoke_tokens(db, user)
    audit(
        db, "user.tokens.revoke", actor_user_id=admin.id, resource_type="user", resource_id=user.id
    )
    db.commit()


@api_router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, db: DbSession, user: CurrentUser) -> ProjectRead:
    project = Project(name=body.name, description=body.description, created_by=user.id)
    db.add(project)
    try:
        db.flush()
        db.add(
            ProjectMember(
                project_id=project.id, user_id=user.id, role=ProjectRole.OWNER, created_by=user.id
            )
        )
        audit(
            db,
            "project.create",
            actor_user_id=user.id,
            project_id=project.id,
            resource_type="project",
            resource_id=project.id,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Project name already exists") from exc
    db.refresh(project)
    return project_response(db, user, project)


@api_router.get("/projects", response_model=list[ProjectRead])
def list_projects(db: DbSession, user: CurrentUser) -> list[ProjectRead]:
    query = select(Project).order_by(Project.created_at.desc())
    allowed = accessible_project_ids(db, user)
    if allowed is not None:
        query = query.where(Project.id.in_(allowed))
    return [project_response(db, user, project) for project in db.scalars(query)]


@api_router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, db: DbSession, user: CurrentUser) -> ProjectRead:
    return project_response(db, user, require_project(db, user, project_id))


@api_router.get("/projects/{project_id}/members", response_model=list[ProjectMemberRead])
def list_project_members(
    project_id: str, db: DbSession, user: CurrentUser
) -> list[ProjectMemberRead]:
    require_project(db, user, project_id)
    rows = db.execute(
        select(ProjectMember, User.username)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(User.username)
    ).all()
    return [
        ProjectMemberRead(
            project_id=member.project_id,
            user_id=member.user_id,
            username=username,
            role=member.role,
            created_at=member.created_at,
        )
        for member, username in rows
    ]


@api_router.put("/projects/{project_id}/members/{user_id}", response_model=ProjectMemberRead)
def set_project_member(
    project_id: str, user_id: str, body: MemberSet, db: DbSession, actor: CurrentUser
) -> ProjectMemberRead:
    require_project(db, actor, project_id, ProjectRole.OWNER)
    target = db.get(User, user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found")
    member = db.get(ProjectMember, (project_id, user_id))
    if member is None:
        member = ProjectMember(
            project_id=project_id, user_id=user_id, role=body.role, created_by=actor.id
        )
        db.add(member)
    else:
        member.role = body.role
    audit(
        db,
        "project.member.set",
        actor_user_id=actor.id,
        project_id=project_id,
        resource_type="user",
        resource_id=user_id,
        details={"role": body.role},
    )
    db.commit()
    db.refresh(member)
    return ProjectMemberRead(
        project_id=project_id,
        user_id=user_id,
        username=target.username,
        role=member.role,
        created_at=member.created_at,
    )


@api_router.delete(
    "/projects/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_project_member(project_id: str, user_id: str, db: DbSession, actor: CurrentUser) -> None:
    require_project(db, actor, project_id, ProjectRole.OWNER)
    member = db.get(ProjectMember, (project_id, user_id))
    if member is None:
        raise HTTPException(status_code=404, detail="Project member not found")
    if member.role == ProjectRole.OWNER:
        owner_count = db.scalar(
            select(func.count())
            .select_from(ProjectMember)
            .where(
                ProjectMember.project_id == project_id,
                ProjectMember.role == ProjectRole.OWNER,
            )
        )
        if owner_count == 1:
            raise HTTPException(status_code=409, detail="A project must keep at least one owner")
    db.delete(member)
    audit(
        db,
        "project.member.remove",
        actor_user_id=actor.id,
        project_id=project_id,
        resource_type="user",
        resource_id=user_id,
    )
    db.commit()


@api_router.post(
    "/nodes/register", response_model=NodeEnrollment, status_code=status.HTTP_201_CREATED
)
def register_node(body: NodeRegister, db: DbSession, _: PlatformActor) -> NodeEnrollment:
    if db.scalar(select(ComputeNode.id).where(ComputeNode.name == body.name)) is not None:
        raise HTTPException(status_code=409, detail="A compute node with this name already exists")
    raw_token = issue_node_token()
    node = ComputeNode(
        name=body.name,
        token_hash=hash_opaque_token(raw_token),
        hostname=body.hostname,
        operating_system=body.operating_system,
        agent_version=body.agent_version,
        status=NodeStatus.OFFLINE,
        capabilities=body.capabilities,
    )
    db.add(node)
    try:
        db.flush()
        audit(db, "node.register", resource_type="node", resource_id=node.id)
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="A compute node with this name already exists"
        ) from error
    db.refresh(node)
    return NodeEnrollment(node=node_response(db, node), node_token=raw_token)


@api_router.post("/nodes/{node_id}/rotate-token", response_model=NodeEnrollment)
def rotate_node_token(node_id: str, db: DbSession, admin: AdminUser) -> NodeEnrollment:
    node = db.get(ComputeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Compute node not found")
    raw_token = issue_node_token()
    node.token_hash = hash_opaque_token(raw_token)
    audit(
        db,
        "node.token.rotate",
        actor_user_id=admin.id,
        resource_type="node",
        resource_id=node.id,
    )
    db.commit()
    db.refresh(node)
    return NodeEnrollment(node=node_response(db, node), node_token=raw_token)


@api_router.post("/nodes/{node_id}/heartbeat", response_model=NodeRead)
def heartbeat_node(node_id: str, body: NodeHeartbeat, db: DbSession, node: NodeActor) -> NodeRead:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    if node.status not in {NodeStatus.DISABLED, NodeStatus.DRAINING}:
        node.status = NodeStatus.ONLINE
    node.capabilities = body.capabilities
    node.last_heartbeat_at = datetime.now(UTC)
    metric = NodeMetric(node_id=node.id, **body.metric.model_dump())
    db.add(metric)
    cutoff = datetime.now(UTC) - timedelta(days=get_settings().metrics_retention_days)
    db.execute(delete(NodeMetric).where(NodeMetric.collected_at < cutoff))
    db.commit()
    db.refresh(node)
    return node_response(db, node)


@api_router.get("/nodes", response_model=list[NodeRead])
def list_nodes(db: DbSession, _: AdminUser) -> list[NodeRead]:
    nodes = db.scalars(select(ComputeNode).order_by(ComputeNode.name))
    return [node_response(db, node) for node in nodes]


@api_router.patch("/nodes/{node_id}", response_model=NodeRead)
def update_node(node_id: str, body: NodeUpdate, db: DbSession, admin: AdminUser) -> NodeRead:
    node = db.get(ComputeNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Compute node not found")
    node.status = body.status
    audit(
        db,
        "node.status.update",
        actor_user_id=admin.id,
        resource_type="node",
        resource_id=node.id,
        details={"status": body.status},
    )
    db.commit()
    db.refresh(node)
    return node_response(db, node)


@api_router.get("/nodes/{node_id}/metrics", response_model=list[NodeMetricRead])
def list_node_metrics(
    node_id: str, db: DbSession, _: AdminUser, minutes: int = 60, limit: int = 720
) -> list[NodeMetric]:
    if db.get(ComputeNode, node_id) is None:
        raise HTTPException(status_code=404, detail="Compute node not found")
    minutes = max(1, min(minutes, get_settings().metrics_retention_days * 24 * 60))
    limit = max(1, min(limit, 2000))
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = list(
        db.scalars(
            select(NodeMetric)
            .where(NodeMetric.node_id == node_id, NodeMetric.collected_at >= cutoff)
            .order_by(NodeMetric.collected_at.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return rows


@api_router.post("/nodes/{node_id}/jobs/claim", response_model=JobRead | None)
def claim_job(node_id: str, body: WorkerClaim, db: DbSession, node: NodeActor) -> Job | None:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().node_offline_seconds)
    if (
        node.status != NodeStatus.ONLINE
        or node.last_heartbeat_at is None
        or aware(node.last_heartbeat_at) < cutoff
    ):
        raise HTTPException(status_code=409, detail="Node is not available for new jobs")
    advertised = (node.capabilities or {}).get("executors", [])
    if not isinstance(advertised, list) or not set(body.supported_kinds).issubset(advertised):
        raise HTTPException(status_code=422, detail="Worker requested an unadvertised job kind")
    now = datetime.now(UTC)
    expired = list(
        db.scalars(
            select(Job).where(
                Job.lease_expires_at < now,
                Job.status.in_(
                    [
                        JobStatus.ASSIGNED,
                        JobStatus.PREPARING,
                        JobStatus.RUNNING,
                        JobStatus.CANCELLING,
                    ]
                ),
            )
        )
    )
    for stale in expired:
        if stale.status == JobStatus.ASSIGNED:
            stale.status = JobStatus.QUEUED
            stale.node_id = None
            stale.assigned_at = None
            stale.lease_expires_at = None
            append_job_event(
                db,
                stale,
                kind="status",
                message="Assignment lease expired; job requeued",
                payload={"status": JobStatus.QUEUED},
            )
        else:
            stale.status = JobStatus.UNKNOWN
            stale.finished_at = now
            stale.lease_expires_at = None
            stale.error_message = "Worker lease expired while the job was active"
            append_job_event(
                db,
                stale,
                kind="status",
                message=stale.error_message,
                payload={"status": JobStatus.UNKNOWN},
            )
    latest_metric = db.scalar(
        select(NodeMetric)
        .where(NodeMetric.node_id == node.id)
        .order_by(NodeMetric.collected_at.desc())
        .limit(1)
    )
    candidates = list(
        db.scalars(
            select(Job)
            .where(Job.status == JobStatus.QUEUED, Job.kind.in_(body.supported_kinds))
            .order_by(Job.priority.desc(), Job.created_at)
            .limit(100)
        )
    )
    lease_expires_at = now + timedelta(seconds=get_settings().job_lease_seconds)
    for candidate in candidates:
        if not job_fits_node(candidate, node, latest_metric):
            continue
        claimed = db.execute(
            update(Job)
            .where(Job.id == candidate.id, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.ASSIGNED,
                node_id=node.id,
                assigned_at=now,
                lease_expires_at=lease_expires_at,
                attempt=Job.attempt + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            continue
        db.expire_all()
        job = db.get(Job, candidate.id)
        append_job_event(
            db,
            job,
            kind="status",
            message=f"Job assigned to {node.name}",
            payload={"status": JobStatus.ASSIGNED, "node_id": node.id},
        )
        db.commit()
        db.refresh(job)
        return job
    db.commit()
    return None


@api_router.post("/nodes/{node_id}/jobs/{job_id}/start", response_model=JobRead)
def start_job(node_id: str, job_id: str, db: DbSession, node: NodeActor) -> Job:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    job = assigned_job(db, node, job_id)
    if job.status == JobStatus.CANCELLING:
        raise HTTPException(status_code=409, detail="Job cancellation is already requested")
    if job.status != JobStatus.ASSIGNED:
        raise HTTPException(status_code=409, detail=f"Cannot start job in state '{job.status}'")
    job.status = JobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=get_settings().job_lease_seconds)
    append_job_event(
        db, job, kind="status", message="Job started", payload={"status": JobStatus.RUNNING}
    )
    db.commit()
    db.refresh(job)
    return job


@api_router.post("/nodes/{node_id}/jobs/{job_id}/events", response_model=JobEventRead)
def create_worker_job_event(
    node_id: str,
    job_id: str,
    body: WorkerJobEventCreate,
    db: DbSession,
    node: NodeActor,
) -> JobEvent:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    job = assigned_job(db, node, job_id)
    if job.status not in {JobStatus.RUNNING, JobStatus.CANCELLING}:
        raise HTTPException(status_code=409, detail="Job is not running")
    event = append_job_event(db, job, kind=body.kind, message=body.message, payload=body.payload)
    db.commit()
    db.refresh(event)
    return event


@api_router.post("/nodes/{node_id}/jobs/{job_id}/control", response_model=WorkerJobControl)
def worker_job_control(
    node_id: str, job_id: str, db: DbSession, node: NodeActor
) -> WorkerJobControl:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    job = assigned_job(db, node, job_id)
    if job.status not in {JobStatus.ASSIGNED, JobStatus.RUNNING, JobStatus.CANCELLING}:
        raise HTTPException(status_code=409, detail="Job is no longer active")
    job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=get_settings().job_lease_seconds)
    db.commit()
    return WorkerJobControl(
        cancel_requested=job.status == JobStatus.CANCELLING,
        lease_expires_at=job.lease_expires_at,
    )


@api_router.post("/nodes/{node_id}/jobs/{job_id}/complete", response_model=JobRead)
def complete_job(
    node_id: str,
    job_id: str,
    body: WorkerJobComplete,
    db: DbSession,
    node: NodeActor,
) -> Job:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    job = assigned_job(db, node, job_id)
    if job.status not in {JobStatus.ASSIGNED, JobStatus.RUNNING, JobStatus.CANCELLING}:
        raise HTTPException(status_code=409, detail="Job is no longer active")
    job.status = JobStatus.CANCELLED if job.status == JobStatus.CANCELLING else body.status
    job.finished_at = datetime.now(UTC)
    job.lease_expires_at = None
    job.exit_code = body.exit_code
    job.result_manifest = body.result_manifest
    job.error_message = body.error_message
    append_job_event(
        db,
        job,
        kind="status",
        message=f"Job {job.status}",
        payload={"status": job.status, "exit_code": body.exit_code},
    )
    db.commit()
    db.refresh(job)
    return job


@api_router.put(
    "/nodes/{node_id}/jobs/{job_id}/artifacts/{relative_path:path}",
    response_model=JobArtifactRead,
)
async def upload_job_artifact(
    node_id: str,
    job_id: str,
    relative_path: str,
    request: Request,
    db: DbSession,
    node: NodeActor,
) -> JobArtifact:
    if node.id != node_id:
        raise HTTPException(status_code=403, detail="Node credential does not match resource")
    job = assigned_job(db, node, job_id)
    if job.status not in {JobStatus.RUNNING, JobStatus.CANCELLING}:
        raise HTTPException(status_code=409, detail="Job is not accepting artifacts")
    relative_path = artifact_relative_path(relative_path)
    settings = get_settings()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.artifact_max_bytes:
                raise HTTPException(status_code=413, detail="Artifact exceeds configured limit")
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from error
    artifact_id = new_id()
    root = settings.artifact_root.resolve()
    target_dir = root / job.id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / artifact_id
    temporary = target.with_suffix(".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.artifact_max_bytes:
                    raise HTTPException(status_code=413, detail="Artifact exceeds configured limit")
                digest.update(chunk)
                handle.write(chunk)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    existing = db.scalar(
        select(JobArtifact).where(
            JobArtifact.job_id == job.id, JobArtifact.relative_path == relative_path
        )
    )
    if existing is not None:
        (root / existing.storage_key).unlink(missing_ok=True)
        db.delete(existing)
        db.flush()
    artifact = JobArtifact(
        id=artifact_id,
        job_id=job.id,
        relative_path=relative_path,
        storage_key=str(target.relative_to(root)).replace("\\", "/"),
        size_bytes=size,
        sha256=digest.hexdigest(),
        content_type=request.headers.get("content-type", "application/octet-stream")[:160],
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


@api_router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(body: JobCreate, db: DbSession, user: CurrentUser) -> Job:
    require_project(db, user, body.project_id, ProjectRole.ANALYST)
    job = Job(
        project_id=body.project_id,
        kind=body.kind,
        payload=body.payload,
        requested_resources=body.requested_resources,
        priority=body.priority,
        created_by=user.id,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    db.flush()
    append_job_event(
        db, job, kind="status", message="Job queued", payload={"status": JobStatus.QUEUED}
    )
    audit(
        db,
        "job.create",
        actor_user_id=user.id,
        project_id=job.project_id,
        resource_type="job",
        resource_id=job.id,
    )
    db.commit()
    db.refresh(job)
    return job


@api_router.get("/job-kinds", response_model=list[str])
def list_job_kinds(db: DbSession, _: CurrentUser) -> list[str]:
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().node_offline_seconds)
    nodes = db.scalars(
        select(ComputeNode).where(
            ComputeNode.status == NodeStatus.ONLINE,
            ComputeNode.last_heartbeat_at >= cutoff,
        )
    )
    kinds: set[str] = set()
    for node in nodes:
        values = (node.capabilities or {}).get("executors", [])
        if isinstance(values, list):
            kinds.update(value for value in values if isinstance(value, str) and value)
    return sorted(kinds)


@api_router.get("/jobs", response_model=list[JobRead])
def list_jobs(db: DbSession, user: CurrentUser, project_id: str | None = None) -> list[Job]:
    query = select(Job).order_by(Job.created_at.desc())
    if project_id is not None:
        require_project(db, user, project_id)
        query = query.where(Job.project_id == project_id)
    else:
        allowed = accessible_project_ids(db, user)
        if allowed is not None:
            query = query.where(Job.project_id.in_(allowed))
    return list(db.scalars(query))


@api_router.get("/jobs/{job_id}", response_model=JobRead)
def get_job(job_id: str, db: DbSession, user: CurrentUser) -> Job:
    job = get_job_or_404(db, job_id)
    require_project(db, user, job.project_id)
    return job


@api_router.post("/jobs/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: str, db: DbSession, user: CurrentUser) -> Job:
    job = get_job_or_404(db, job_id)
    require_project(db, user, job.project_id, ProjectRole.ANALYST)
    job = request_job_cancellation(db, job)
    audit(
        db,
        "job.cancel",
        actor_user_id=user.id,
        project_id=job.project_id,
        resource_type="job",
        resource_id=job.id,
    )
    db.commit()
    db.refresh(job)
    return job


@api_router.get("/jobs/{job_id}/events", response_model=list[JobEventRead])
def list_job_events(
    job_id: str, db: DbSession, user: CurrentUser, limit: int = 1000
) -> list[JobEvent]:
    job = get_job_or_404(db, job_id)
    require_project(db, user, job.project_id)
    limit = max(1, min(limit, 2000))
    rows = list(
        db.scalars(
            select(JobEvent)
            .where(JobEvent.job_id == job_id)
            .order_by(JobEvent.sequence.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return rows


@api_router.get("/jobs/{job_id}/artifacts", response_model=list[JobArtifactRead])
def list_job_artifacts(job_id: str, db: DbSession, user: CurrentUser) -> list[JobArtifact]:
    job = get_job_or_404(db, job_id)
    require_project(db, user, job.project_id)
    return list(
        db.scalars(
            select(JobArtifact)
            .where(JobArtifact.job_id == job_id)
            .order_by(JobArtifact.relative_path)
        )
    )


@api_router.get("/jobs/{job_id}/artifacts/{artifact_id}", response_class=FileResponse)
def download_job_artifact(
    job_id: str, artifact_id: str, db: DbSession, user: CurrentUser
) -> FileResponse:
    job = get_job_or_404(db, job_id)
    require_project(db, user, job.project_id)
    artifact = db.get(JobArtifact, artifact_id)
    if artifact is None or artifact.job_id != job.id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    root = get_settings().artifact_root.resolve()
    target = (root / artifact.storage_key).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact file not found")
    return FileResponse(
        target,
        media_type=artifact.content_type,
        filename=PurePosixPath(artifact.relative_path).name,
    )


@api_router.get("/audit-logs", response_model=list[AuditLogRead])
def list_audit_logs(
    db: DbSession, user: CurrentUser, project_id: str | None = None, limit: int = 100
) -> list[AuditLog]:
    limit = max(1, min(limit, 500))
    query = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if project_id is not None:
        require_project(db, user, project_id)
        query = query.where(AuditLog.project_id == project_id)
    elif not user.is_admin:
        raise HTTPException(status_code=403, detail="Choose a project to view its audit log")
    return list(db.scalars(query))
