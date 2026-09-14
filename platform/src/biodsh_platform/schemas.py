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
    project_id: str
    user_id: str
    username: str
    role: ProjectRole
    created_at: datetime


class NodeRegister(WriteModel):
    name: str = Field(min_length=1, max_length=120)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class NodeHeartbeat(WriteModel):
    capabilities: dict[str, Any] = Field(default_factory=dict)


class NodeRead(ApiModel):
    id: str
    name: str
    status: str
    capabilities: dict[str, Any]
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobCreate(WriteModel):
    project_id: str
    kind: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_resources: dict[str, Any] = Field(default_factory=dict)


class JobRead(ApiModel):
    id: str
    project_id: str
    node_id: str | None
    kind: str
    status: str
    payload: dict[str, Any]
    requested_resources: dict[str, Any]
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
