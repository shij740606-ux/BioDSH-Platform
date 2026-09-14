from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biodsh_platform.models import ComputeNode, Job, JobEvent, JobStatus, NodeMetric

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
    if current == JobStatus.QUEUED:
        job.finished_at = datetime.now(UTC)
    append_job_event(
        db,
        job,
        kind="status",
        message="Cancellation requested",
        payload={"status": job.status},
    )
    return job


def job_fits_node(job: Job, node: ComputeNode, metric: NodeMetric | None) -> bool:
    requested = job.requested_resources or {}
    capabilities = node.capabilities or {}
    cpu_total = int(capabilities.get("cpu_logical") or 0)
    memory_free = (
        0 if metric is None else max(0, metric.memory_total_bytes - metric.memory_used_bytes)
    )
    disk_free = 0 if metric is None else max(0, metric.disk_total_bytes - metric.disk_used_bytes)
    gpus = [] if metric is None else metric.gpus
    if int(requested.get("cpu", 0)) > cpu_total:
        return False
    if int(requested.get("memory_bytes", 0)) > memory_free:
        return False
    if int(requested.get("disk_bytes", 0)) > disk_free:
        return False
    gpu_count = int(requested.get("gpu_count", 0))
    if gpu_count > len(gpus):
        return False
    required_gpu_memory = int(requested.get("gpu_memory_mib", 0))
    if required_gpu_memory:

        def numeric(value: object) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        available = sorted(
            (
                max(
                    0,
                    numeric(gpu.get("memory_total_mib")) - numeric(gpu.get("memory_used_mib")),
                )
                for gpu in gpus
            ),
            reverse=True,
        )
        required_count = max(1, gpu_count)
        if len(available) < required_count or any(
            amount < required_gpu_memory for amount in available[:required_count]
        ):
            return False
    return True
