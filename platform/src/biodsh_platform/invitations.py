import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from biodsh_platform.audit import audit
from biodsh_platform.authorization import require_project
from biodsh_platform.config import get_settings
from biodsh_platform.db import get_db
from biodsh_platform.models import ProjectInvitation, ProjectMember, ProjectRole, User
from biodsh_platform.security import CurrentUser, hash_opaque_token

router = APIRouter(tags=["invitations"])
Db = Annotated[Session, Depends(get_db)]


class InviteCreate(BaseModel):
    role: Literal["owner", "analyst", "viewer"] = "analyst"
    days: int = Field(default=7, ge=1, le=30)
    uses: int = Field(default=1, ge=1, le=100)


@router.post("/projects/{project_id}/invitations")
def create(project_id: str, body: InviteCreate, db: Db, user: CurrentUser):
    require_project(db, user, project_id, ProjectRole.OWNER)
    if not get_settings().public_base_url:
        raise HTTPException(503, "管理员尚未配置平台公开地址")
    token = secrets.token_urlsafe(48)
    row = ProjectInvitation(
        token_hash=hash_opaque_token(token),
        project_id=project_id,
        role=body.role,
        expires_at=datetime.now(UTC) + timedelta(days=body.days),
        remaining_uses=body.uses,
        created_by=user.id,
    )
    db.add(row)
    audit(db, "project.invitation.create", actor_user_id=user.id, project_id=project_id)
    db.commit()
    return {
        "url": get_settings().public_base_url.rstrip("/") + "/join#" + token,
        "expires_at": row.expires_at,
        "id": row.token_hash,
    }


@router.get("/projects/{project_id}/invitations")
def list_invites(project_id: str, db: Db, user: CurrentUser):
    require_project(db, user, project_id, ProjectRole.OWNER)
    return [
        dict(
            id=r.token_hash,
            role=r.role,
            expires_at=r.expires_at,
            remaining_uses=r.remaining_uses,
            revoked=r.revoked,
        )
        for r in db.scalars(
            select(ProjectInvitation).where(ProjectInvitation.project_id == project_id)
        )
    ]


class Revoke(BaseModel):
    id: str = Field(min_length=64, max_length=64)


@router.post("/projects/{project_id}/invitations/revoke")
def revoke(project_id: str, body: Revoke, db: Db, user: CurrentUser):
    require_project(db, user, project_id, ProjectRole.OWNER)
    row = db.get(ProjectInvitation, body.id)
    if not row or row.project_id != project_id:
        raise HTTPException(404, "邀请不存在")
    row.revoked = True
    audit(db, "project.invitation.revoke", actor_user_id=user.id, project_id=project_id)
    db.commit()
    return {"revoked": True}


def validate(db: Session, hashed: str):
    row = db.get(ProjectInvitation, hashed)
    if (
        not row
        or row.revoked
        or row.remaining_uses <= 0
        or row.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
    ):
        raise HTTPException(400, "邀请已过期、撤销或使用完毕")
    return row


def accept(db: Session, hashed: str, user: User):
    row = validate(db, hashed)
    if db.get(ProjectMember, (row.project_id, user.id)):
        return row.project_id
    changed = db.execute(
        update(ProjectInvitation)
        .where(
            ProjectInvitation.token_hash == hashed,
            ProjectInvitation.revoked.is_(False),
            ProjectInvitation.remaining_uses > 0,
            ProjectInvitation.expires_at > datetime.now(UTC),
        )
        .values(remaining_uses=ProjectInvitation.remaining_uses - 1)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise HTTPException(409, "邀请已被使用")
    db.add(
        ProjectMember(
            project_id=row.project_id, user_id=user.id, role=row.role, created_by=row.created_by
        )
    )
    audit(db, "project.invitation.accept", actor_user_id=user.id, project_id=row.project_id)
    return row.project_id


class Token(BaseModel):
    token: str = Field(min_length=32, max_length=200)


@router.post("/invitations/accept")
def accept_current(body: Token, db: Db, user: CurrentUser):
    pid = accept(db, hash_opaque_token(body.token), user)
    db.commit()
    return {"project_id": pid}


@router.get("/join", response_class=HTMLResponse)
def landing():
    return HTMLResponse(
        "<meta charset=utf-8><h2>加入 BioDSH 项目</h2>"
        "<p>复制当前完整邀请链接，在 BioDSH 的设置中选择‘使用项目邀请’。"
        "平台地址将自动识别，随后通过飞书登录加入项目。</p>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
