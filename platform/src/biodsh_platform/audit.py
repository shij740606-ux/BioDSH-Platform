from sqlalchemy.orm import Session

from biodsh_platform.models import AuditLog


def audit(
    db: Session,
    action: str,
    *,
    actor_user_id: str | None = None,
    outcome: str = "success",
    project_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            outcome=outcome,
            project_id=project_id,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
    )
