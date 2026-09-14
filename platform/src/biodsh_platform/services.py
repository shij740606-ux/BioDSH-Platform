from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biodsh_platform.models import Job, JobEvent, JobStatus

CANCELLABLE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.ASSIGNED,
    JobStatus.PREPARING,
    JobStatus.RUNNING,
}


def get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


def append_job_event(
    db: Session,
    job: Job,
    *,
    kind: str,
    message: str,
    payload: dict | None = None,
) -> JobEvent:
    current = db.scalar(select(func.max(JobEvent.sequence)).where(JobEvent.job_id == job.id))
    event = JobEvent(
        job_id=job.id,
        sequence=(current or 0) + 1,
        kind=kind,
        message=message,
        payload=payload or {},
    )
    db.add(event)
    return event


def request_job_cancellation(db: Session, job: Job) -> Job:
    current = JobStatus(job.status)
    if current not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job in state '{current}' cannot be cancelled",
        )
    job.status = JobStatus.CANCELLED if current == JobStatus.QUEUED else JobStatus.CANCELLING
    append_job_event(
        db,
        job,
        kind="status",
        message="Cancellation requested",
        payload={"status": job.status},
    )
    return job
