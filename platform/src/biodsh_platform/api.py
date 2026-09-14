from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, func, select, text
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
    JobEvent,
    JobStatus,
    NodeMetric,
    NodeStatus,
    Project,
    ProjectMember,
    ProjectRole,
    RefreshToken,
    User,
)
from biodsh_platform.schemas import (
    AuditLogRead,
    HealthRead,
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
from biodsh_platform.services import append_job_event, get_job_or_404, request_job_cancellation

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


@api_router.post("/jobs", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(body: JobCreate, db: DbSession, user: CurrentUser) -> Job:
    require_project(db, user, body.project_id, ProjectRole.ANALYST)
    job = Job(
        project_id=body.project_id,
        kind=body.kind,
        payload=body.payload,
        requested_resources=body.requested_resources,
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
def list_job_events(job_id: str, db: DbSession, user: CurrentUser) -> list[JobEvent]:
    job = get_job_or_404(db, job_id)
    require_project(db, user, job.project_id)
    return list(
        db.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.sequence))
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
