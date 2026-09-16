from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from biodsh_platform.models import ProjectRole


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WriteModel(ApiModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class LoginRequest(WriteModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(char.isalnum() or char in "._@+-" for char in value):
            raise ValueError("username contains unsupported characters")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if value.isspace():
            raise ValueError("password must not contain only whitespace")
        return value


class RefreshRequest(WriteModel):
    refresh_token: str = Field(min_length=32, max_length=512)


class UserCreate(LoginRequest):
    is_admin: bool = False


class UserUpdate(WriteModel):
    is_active: bool | None = None
    is_admin: bool | None = None


class UserRead(ApiModel):
    display_name: str = ""
    avatar_url: str = ""
    id: str
    username: str
    is_active: bool
    is_admin: bool
    last_login_at: datetime | None
    created_at: datetime


class TokenRead(ApiModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserRead


class ProjectCreate(WriteModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class ProjectRead(ApiModel):
    id: str
    name: str
    description: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    role: ProjectRole | None = None


class MemberSet(WriteModel):
    role: Literal[ProjectRole.OWNER, ProjectRole.ANALYST, ProjectRole.VIEWER]


class ProjectMemberRead(ApiModel):
    display_name: str = ""
    avatar_url: str = ""
    project_id: str
    user_id: str
    username: str
    role: ProjectRole
    created_at: datetime


class NodeRegister(WriteModel):
    name: str = Field(min_length=1, max_length=120)
    hostname: str = Field(default="", max_length=255)
    operating_system: str = Field(default="", max_length=120)
    agent_version: str = Field(default="", max_length=40)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Node name cannot be empty")
        return value


class NodeMetricCreate(WriteModel):
    cpu_percent: float = Field(ge=0, le=100)
    load_one: float | None = Field(default=None, ge=0)
    memory_used_bytes: int = Field(ge=0)
    memory_total_bytes: int = Field(ge=0)
    memory_percent: float = Field(ge=0, le=100)
    disk_used_bytes: int = Field(ge=0)
    disk_total_bytes: int = Field(ge=0)
    disk_percent: float = Field(ge=0, le=100)
    network_rx_bytes: int = Field(ge=0)
    network_tx_bytes: int = Field(ge=0)
    running_jobs: int = Field(default=0, ge=0)
    gpus: list[dict[str, Any]] = Field(default_factory=list, max_length=32)


class NodeHeartbeat(WriteModel):
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metric: NodeMetricCreate


class NodeMetricRead(NodeMetricCreate):
    id: int
    node_id: str
    collected_at: datetime


class NodeUpdate(WriteModel):
    status: Literal["online", "draining", "disabled"]


class NodeRead(ApiModel):
    id: str
    name: str
    status: str
    hostname: str
    operating_system: str
    agent_version: str
    capabilities: dict[str, Any]
    latest_metric: NodeMetricRead | None = None
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NodeEnrollment(ApiModel):
    node: NodeRead
    node_token: str


class JobCreate(WriteModel):
    node_ids: list[str] | None = Field(default=None, min_length=1, max_length=200)
    project_id: str
    kind: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_resources: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Job kind cannot be empty")
        return value

    @field_validator("requested_resources")
    @classmethod
    def validate_resources(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {"cpu", "memory_bytes", "disk_bytes", "gpu_count", "gpu_memory_mib"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unsupported resource fields: {', '.join(sorted(unknown))}")
        normalized: dict[str, int] = {}
        for key, raw in value.items():
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(f"Resource '{key}' must be a non-negative integer")
            normalized[key] = raw
        return normalized


class JobRead(ApiModel):
    id: str
    project_id: str
    node_id: str | None
    kind: str
    status: str
    payload: dict[str, Any]
    requested_resources: dict[str, Any]
    priority: int
    attempt: int
    assigned_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    lease_expires_at: datetime | None
    exit_code: int | None
    result_manifest: dict[str, Any]
    error_message: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class JobEventRead(ApiModel):
    id: int
    job_id: str
    sequence: int
    kind: str
    message: str
    payload: dict[str, Any]
    created_at: datetime


class WorkerClaim(WriteModel):
    supported_kinds: list[str] = Field(min_length=1, max_length=200)

    @field_validator("supported_kinds")
    @classmethod
    def validate_kinds(cls, value: list[str]) -> list[str]:
        kinds = list(dict.fromkeys(kind.strip() for kind in value if kind.strip()))
        if not kinds or any(len(kind) > 120 for kind in kinds):
            raise ValueError("supported_kinds contains an invalid job kind")
        return kinds


class WorkerJobEventCreate(WriteModel):
    kind: Literal["log", "progress"] = "log"
    message: str = Field(max_length=8000)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkerJobComplete(WriteModel):
    status: Literal["succeeded", "failed", "cancelled"]
    exit_code: int | None = None
    error_message: str = Field(default="", max_length=4000)
    result_manifest: dict[str, Any] = Field(default_factory=dict)


class WorkerJobControl(ApiModel):
    cancel_requested: bool
    lease_expires_at: datetime


class JobArtifactRead(ApiModel):
    id: str
    job_id: str
    relative_path: str
    size_bytes: int
    sha256: str
    content_type: str
    created_at: datetime


class AuditLogRead(ApiModel):
    id: str
    actor_user_id: str | None
    action: str
    outcome: str
    project_id: str | None
    resource_type: str | None
    resource_id: str | None
    details: dict[str, Any]
    created_at: datetime


class HealthRead(ApiModel):
    status: str
    service: str
    version: str
    database: str


class PlatformMetadata(ApiModel):
    service: str
    version: str
    api_versions: dict[str, str]
    health: dict[str, str]
    capabilities: list[str] = Field(default_factory=list)
