from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from biodsh_platform.models import Project, ProjectMember, ProjectRole, User

ROLE_LEVEL = {
    ProjectRole.VIEWER: 10,
    ProjectRole.ANALYST: 20,
    ProjectRole.OWNER: 30,
    ProjectRole.ADMIN: 40,
}


def project_role(db: Session, user: User, project_id: str) -> ProjectRole | None:
    if user.is_admin:
        return ProjectRole.ADMIN
    role = db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user.id
        )
    )
    return ProjectRole(role) if role else None


def require_project(
    db: Session, user: User, project_id: str, minimum: ProjectRole = ProjectRole.VIEWER
) -> Project:
    project = db.get(Project, project_id)
    role = project_role(db, user, project_id) if project else None
    if project is None or role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if ROLE_LEVEL[role] < ROLE_LEVEL[minimum]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Project role is insufficient"
        )
    return project


def accessible_project_ids(db: Session, user: User) -> list[str] | None:
    if user.is_admin:
        return None
    return list(
        db.scalars(select(ProjectMember.project_id).where(ProjectMember.user_id == user.id))
    )
