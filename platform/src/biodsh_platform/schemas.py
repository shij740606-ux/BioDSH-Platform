from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


class ProjectRead(ApiModel):
    id: str
    name: str
    description: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class NodeRegister(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class NodeHeartbeat(ApiModel):
    capabilities: dict[str, Any] = Field(default_factory=dict)


class NodeRead(ApiModel):
    id: str
    name: str
    status: str
    capabilities: dict[str, Any]
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobCreate(ApiModel):
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


class HealthRead(ApiModel):
    status: str
    service: str
    version: str
    database: str
