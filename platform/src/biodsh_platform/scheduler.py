"""Single control-plane scheduler; one active attempt per current serial Worker."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from biodsh_platform.config import get_settings
from biodsh_platform.models import ComputeNode, Job, JobStatus, NodeMetric, NodeStatus
from biodsh_platform.services import job_fits_node


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


RESERVED = [
    JobStatus.ASSIGNED,
    JobStatus.PREPARING,
    JobStatus.RUNNING,
    JobStatus.CANCELLING,
    JobStatus.UNKNOWN,
]


def evaluate(db: Session, job: Job) -> dict:
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().node_offline_seconds)
    scope = (job.payload or {}).get("_scheduling", {}).get("node_ids")
    rows = []
    for node in db.scalars(select(ComputeNode).order_by(ComputeNode.id)):
        if scope is not None and node.id not in scope:
            continue
        metric = db.scalar(
            select(NodeMetric)
            .where(NodeMetric.node_id == node.id)
            .order_by(NodeMetric.collected_at.desc())
            .limit(1)
        )
        reservations = list(
            db.scalars(select(Job).where(Job.node_id == node.id, Job.status.in_(RESERVED)))
        )
        reason = ""
        if (
            node.status != NodeStatus.ONLINE
            or not node.last_heartbeat_at
            or aware(node.last_heartbeat_at) < cutoff
        ):
            reason = "服务器离线或未启用"
        elif not metric or aware(metric.collected_at) < cutoff:
            reason = "等待新资源采样"
        elif job.kind not in (node.capabilities or {}).get("executors", []):
            reason = "运行环境不支持该任务类型"
        elif reservations or metric.running_jobs:
            reason = "Worker 正在运行任务或有待确认的中断任务"
        elif not job_fits_node(job, node, metric):
            reason = "可用内存、磁盘、CPU 或 GPU 显存不足"
        cpu_total = int((node.capabilities or {}).get("cpu_logical", 0))
        cpu_free = max(0, int(cpu_total * (1 - (metric.cpu_percent if metric else 100) / 100)))
        if not reason and int((job.requested_resources or {}).get("cpu", 0)) > cpu_free:
            reason = "当前空闲 CPU 不足"
        requested = job.requested_resources or {}
        gpu_count = max(
            int(requested.get("gpu_count", 0)), 1 if requested.get("gpu_memory_mib") else 0
        )
        gpu_memory = int(requested.get("gpu_memory_mib", 0))
        available_gpus = (
            []
            if not metric
            else [
                g
                for g in metric.gpus
                if int(g.get("memory_total_mib") or 0) - int(g.get("memory_used_mib") or 0)
                >= gpu_memory
            ]
        )
        available_gpus.sort(key=lambda g: int(g.get("memory_used_mib") or 0))
        gpu_indices = [str(g.get("index", i)) for i, g in enumerate(available_gpus[:gpu_count])]
        rows.append(
            dict(
                node_id=node.id,
                name=node.name,
                gpu_indices=gpu_indices,
                eligible=not reason,
                reason=reason,
                cpu_free=cpu_free,
                memory_free=0
                if not metric
                else max(0, metric.memory_total_bytes - metric.memory_used_bytes),
                cpu_percent=100 if not metric else metric.cpu_percent,
                reserved_cpu=sum(
                    int((j.requested_resources or {}).get("cpu", 0)) for j in reservations
                ),
            )
        )
    eligible = sorted(
        (r for r in rows if r["eligible"]),
        key=lambda r: (r["cpu_percent"], -r["memory_free"], -r["cpu_free"], r["node_id"]),
    )
    chosen = eligible[0] if eligible else None
    return dict(
        selected_node_id=chosen["node_id"] if chosen else None,
        reason=("优先选择 CPU 负载较低且资源充足的服务器：" + chosen["name"])
        if chosen
        else "所选范围暂时没有可用服务器，任务继续排队",
        nodes=rows,
    )
