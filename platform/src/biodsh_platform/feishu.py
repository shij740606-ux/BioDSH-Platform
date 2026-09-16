"""Feishu desktop login: browser callback and one-time desktop polling ticket."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from biodsh_platform.audit import audit
from biodsh_platform.config import get_settings
from biodsh_platform.db import get_db
from biodsh_platform.models import ExternalIdentity, OAuthFlow, User
from biodsh_platform.security import create_access_token, hash_password, issue_refresh_token

router = APIRouter(prefix="/auth/feishu", tags=["feishu"])
Db = Annotated[Session, Depends(get_db)]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def request_api(path: str, body: dict | None = None, token: str = "") -> dict:
    req = Request(
        "https://open.feishu.cn/open-apis/" + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    try:
        with urlopen(req, timeout=15) as response:
            value = json.load(response)
        if value.get("code", 0) != 0 or value.get("error"):
            raise ValueError("Provider error")
        return value
    except Exception as exc:
        raise HTTPException(502, "飞书服务请求失败，请联系管理员检查应用配置") from exc


def identity_from_code(code: str) -> dict:
    settings = get_settings()
    token = request_api(
        "authen/v2/oauth/token",
        dict(
            grant_type="authorization_code",
            code=code,
            client_id=settings.feishu_app_id,
            client_secret=settings.feishu_app_secret.get_secret_value(),
            redirect_uri=settings.feishu_redirect_uri,
        ),
    ).get("access_token")
    if not token:
        raise HTTPException(502, "飞书未返回有效登录凭据")
    return request_api("authen/v1/user_info", token=token).get("data", {})


def enabled():
    if not get_settings().feishu_login_enabled:
        raise HTTPException(503, "管理员尚未启用飞书登录")


@router.get("/status")
def status():
    return {"enabled": get_settings().feishu_login_enabled}


@router.post("/start")
def start(db: Db):
    enabled()
    now = datetime.now(UTC)
    db.execute(delete(OAuthFlow).where(OAuthFlow.expires_at < now))
    state, poll = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    db.add(
        OAuthFlow(
            state_hash=digest(state), poll_hash=digest(poll), expires_at=now + timedelta(minutes=5)
        )
    )
    db.commit()
    settings = get_settings()
    return {
        "poll_token": poll,
        "expires_in": 300,
        "authorization_url": "https://accounts.feishu.cn/open-apis/authen/v1/authorize?"
        + urlencode(
            dict(
                client_id=settings.feishu_app_id,
                redirect_uri=settings.feishu_redirect_uri,
                response_type="code",
                state=state,
            )
        ),
    }


@router.get("/callback", response_class=HTMLResponse)
def callback(db: Db, state: str = "", code: str = "", error: str = ""):
    enabled()
    now = datetime.now(UTC)
    claimed = db.execute(
        update(OAuthFlow)
        .where(
            OAuthFlow.state_hash == digest(state),
            OAuthFlow.status == "pending",
            OAuthFlow.expires_at > now,
        )
        .values(status="exchanging")
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(400, "授权已过期或已使用，请重新登录")
    db.commit()
    try:
        if error or not code:
            raise HTTPException(400, "飞书授权已取消")
        identity = identity_from_code(code)
        tenant, external = identity.get("tenant_key"), identity.get("open_id")
        if not tenant or tenant not in get_settings().feishu_allowed_tenants or not external:
            raise HTTPException(403, "此飞书组织未获准使用本平台")
        record = db.get(ExternalIdentity, ("feishu", tenant, external))
        if record:
            user = db.get(User, record.user_id)
        else:
            user = User(
                username="feishu_" + digest(tenant + ":" + external)[:32],
                password_hash=hash_password(secrets.token_urlsafe(48)),
                is_admin=False,
            )
            db.add(user)
            db.flush()
            db.add(
                ExternalIdentity(
                    provider="feishu",
                    tenant_key=tenant,
                    external_id=external,
                    user_id=user.id,
                    display_name=str(identity.get("name", ""))[:120],
                )
            )
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                record = db.get(ExternalIdentity, ("feishu", tenant, external))
                if not record:
                    raise HTTPException(409, "请重新发起登录") from None
                user = db.get(User, record.user_id)
        if not user or not user.is_active:
            raise HTTPException(403, "此账号已被停用")
        user.last_login_at = now
        db.execute(
            update(OAuthFlow)
            .where(OAuthFlow.state_hash == digest(state))
            .values(status="ready", user_id=user.id)
        )
        audit(db, "auth.feishu.authorized", actor_user_id=user.id)
        db.commit()
        return HTMLResponse(
            "<meta charset=utf-8><h2>飞书授权成功</h2><p>请返回 BioDSH 桌面端。</p>",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except Exception:
        db.rollback()
        db.execute(
            update(OAuthFlow).where(OAuthFlow.state_hash == digest(state)).values(status="failed")
        )
        db.commit()
        raise


class Poll(BaseModel):
    poll_token: str = Field(min_length=32, max_length=200)


@router.post("/poll")
def poll(body: Poll, db: Db):
    enabled()
    now = datetime.now(UTC)
    flow = db.scalar(select(OAuthFlow).where(OAuthFlow.poll_hash == digest(body.poll_token)))
    if (
        not flow
        or flow.expires_at.replace(tzinfo=UTC) <= now
        or flow.status in {"failed", "consumed"}
    ):
        raise HTTPException(400, "登录已过期、取消或完成，请重新登录")
    if flow.status != "ready":
        return {"status": "pending"}
    claimed = db.execute(
        update(OAuthFlow)
        .where(OAuthFlow.state_hash == flow.state_hash, OAuthFlow.status == "ready")
        .values(status="consumed")
    )
    if claimed.rowcount != 1:
        raise HTTPException(409, "登录已领取")
    user = db.get(User, flow.user_id)
    if not user or not user.is_active:
        raise HTTPException(403, "账号已停用")
    access, expires = create_access_token(user)
    refresh, _ = issue_refresh_token(db, user)
    audit(db, "auth.feishu.login", actor_user_id=user.id)
    db.commit()
    return {
        "status": "ready",
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires,
        "token_type": "bearer",
    }
