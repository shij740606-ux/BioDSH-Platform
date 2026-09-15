import sys
from pathlib import Path
from typing import Any

from biodsh_platform import worker


def test_worker_executes_profile_streams_log_and_uploads_result(
    tmp_path: Path, monkeypatch
) -> None:
    completed: list[dict[str, Any]] = []
    logs: list[str] = []

    def fake_request(url: str, **kwargs: Any) -> Any:
        if url.endswith("/control"):
            return {"cancel_requested": False}
        if url.endswith("/complete"):
            completed.append(kwargs["body"])
        return {}

    monkeypatch.setattr(worker, "request_json", fake_request)
    monkeypatch.setattr(worker, "send_heartbeat", lambda *args: None)
    monkeypatch.setattr(
        worker,
        "post_job_event",
        lambda settings, credentials, job_id, message: logs.append(message),
    )
    monkeypatch.setattr(
        worker,
        "upload_artifact",
        lambda settings, credentials, job_id, relative_path, source: {
            "relative_path": relative_path,
            "size_bytes": source.stat().st_size,
        },
    )
    settings = worker.WorkerSettings(
        metric_path=tmp_path,
        workspace_root=tmp_path / "jobs",
        executors_file=tmp_path / "executors.json",
        heartbeat_seconds=5,
    )
    script = (
        "from pathlib import Path; "
        "Path('results').mkdir(); "
        "Path('results/out.txt').write_text('ok'); "
        "import os; print('GPU=' + os.environ.get('CUDA_VISIBLE_DEVICES', 'missing'), flush=True); "
        "print('analysis complete', flush=True)"
    )
    executors = {
        "test": {
            "command": [sys.executable, "-u", "-c", script],
            "timeout_seconds": 10,
            "result_globs": ["results/*"],
        }
    }
    credentials = {
        "base_url": "http://platform.test",
        "api_path": "/api/v1",
        "node_id": "node-1",
        "node_token": "token",
    }
    worker.execute_job(
        settings,
        credentials,
        {"cpu_logical": 4},
        executors,
        {
            "id": "job-1",
            "kind": "test",
            "attempt": 1,
            "payload": {"arguments": [], "_allocation": {"gpu_indices": ["2"]}},
        },
    )
    assert "GPU=2" in logs
    assert "analysis complete" in logs
    assert completed[0]["status"] == "succeeded"
    assert completed[0]["exit_code"] == 0
    assert completed[0]["result_manifest"]["artifacts"][0]["relative_path"] == "results/out.txt"


def test_worker_rechecks_resources_before_start(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(
        worker,
        "collect_metric",
        lambda *_: dict(
            memory_total_bytes=100,
            memory_used_bytes=99,
            disk_total_bytes=100,
            disk_used_bytes=0,
            gpus=[],
        ),
    )
    settings = worker.WorkerSettings(metric_path=tmp_path, workspace_root=tmp_path / "jobs")
    with pytest.raises(RuntimeError, match="Resources changed"):
        worker.execute_job(
            settings,
            {},
            {"cpu_logical": 16},
            {},
            dict(kind="test", requested_resources={"memory_bytes": 50}),
        )
    assert not settings.workspace_root.exists()
