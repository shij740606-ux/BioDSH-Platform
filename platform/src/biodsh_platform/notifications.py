"""Database outbox; stable provider UUID prevents duplicates after retries."""

import json
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from biodsh_platform.config import get_settings
from biodsh_platform.db import SessionLocal
from biodsh_platform.feishu import request_api
from biodsh_platform.models import NotificationOutbox, ProjectChat


def tenant_token():
    settings = get_settings()
    return request_api(
        "auth/v3/tenant_access_token/internal",
        dict(
            app_id=settings.feishu_app_id, app_secret=settings.feishu_app_secret.get_secret_value()
        ),
    )["tenant_access_token"]


def card(body: dict) -> dict:
    status = str(body.get("status", "unknown"))
    labels = {
        "running": ("任务已开始", "blue"),
        "succeeded": ("任务已完成", "green"),
        "failed": ("任务失败", "red"),
        "cancelled": ("任务已取消", "grey"),
        "unknown": ("任务状态待确认", "orange"),
    }
    title, template = labels.get(status, ("任务状态更新", "blue"))
    kind = str(body.get("kind", "分析任务"))
    job_id = str(body.get("job_id", ""))
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**分析类型：** {kind}\n**任务编号：** {job_id}\n"
                        f"**当前状态：** {status}\n\n请在 BioDSH 桌面端查看日志和结果。"
                    ),
                },
            }
        ],
    }


def deliver_once():
    if not get_settings().feishu_notifications_enabled:
        return
    with SessionLocal() as db:
        now = datetime.now(UTC)
        row = db.scalar(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.status.in_(["pending", "sending"]),
                NotificationOutbox.available_at <= now,
            )
            .order_by(NotificationOutbox.available_at)
            .limit(1)
        )
        if row is None:
            return
        original = row.available_at
        claimed = db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == row.id,
                NotificationOutbox.available_at == original,
                NotificationOutbox.status.in_(["pending", "sending"]),
            )
            .values(
                status="sending",
                attempts=NotificationOutbox.attempts + 1,
                available_at=now + timedelta(seconds=120),
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return
        db.commit()
        db.refresh(row)
        binding = db.get(ProjectChat, row.project_id)
        if not binding:
            row.status = "cancelled"
            db.commit()
            return
        try:
            request_api(
                "im/v1/messages?receive_id_type=chat_id",
                dict(
                    receive_id=binding.chat_id,
                    msg_type="interactive",
                    content=json.dumps(card(row.body), ensure_ascii=False),
                    uuid=row.id,
                ),
                tenant_token(),
            )
            row.status = "sent"
        except Exception:
            row.status = "failed" if row.attempts >= 10 else "pending"
            row.available_at = now + timedelta(seconds=min(3600, 15 * 2**row.attempts))
        db.commit()


def run(stop: threading.Event):
    while not stop.wait(5):
        try:
            deliver_once()
        except Exception:
            pass  # Next iteration retries; task execution is never blocked.
