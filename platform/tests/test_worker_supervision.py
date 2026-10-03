import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import psutil
import pytest

from biodsh_platform import worker


@pytest.mark.parametrize("cancel_upload", [False, True])
def test_real_process_and_slow_http_upload_keep_lease_and_observe_cancel(
    tmp_path, monkeypatch, cancel_upload
):
    state = {"controls": [], "uploads": [], "completed": None, "expired": False}
    uploading = threading.Event()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/control"):
                with lock:
                    state["controls"].append(time.monotonic())
                self.reply({"cancel_requested": cancel_upload and uploading.is_set()})
            elif self.path.endswith("/complete"):
                state["completed"] = body
                self.reply({})
            else:
                self.reply({})

        def do_PUT(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["uploads"].append(self.path)
            uploading.set()
            # A single blocking upload lasts several simulated lease periods.
            for _ in range(12):
                time.sleep(0.05)
                with lock:
                    if time.monotonic() - state["controls"][-1] > 0.3:
                        state["expired"] = True
            self.reply({"relative_path": "result.txt"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(worker, "send_heartbeat", lambda *args: None)
    settings = worker.WorkerSettings(
        workspace_root=tmp_path / "jobs", cancel_grace_seconds=1
    ).model_copy(update={"heartbeat_seconds": 0.05})
    script = (
        "from pathlib import Path; Path('one.txt').write_text('one'); "
        "Path('two.txt').write_text('two')"
    )
    try:
        worker.execute_job(
            settings,
            {
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "api_path": "/api/v1",
                "node_id": "node",
                "node_token": "test",
            },
            {},
            {"test": {"command": [sys.executable, "-c", script], "result_globs": ["*.txt"]}},
            {"id": "job", "kind": "test", "attempt": 1},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not state["expired"]
    assert state["completed"]["status"] == ("cancelled" if cancel_upload else "succeeded")
    assert len(state["uploads"]) == (1 if cancel_upload else 2)
    assert len(state["controls"]) >= 5


def test_tail_log_block_does_not_block_renewal(tmp_path, monkeypatch):
    renewals = threading.Event()
    blocked = threading.Event()
    completed = []
    original_process = worker.ExecutorProcess

    def exited_process(*args, **kwargs):
        process = original_process(*args, **kwargs)
        process.wait(timeout=5)  # force the final log-drain path
        return process

    def request(url, **kwargs):
        if url.endswith("/control"):
            if blocked.is_set():
                renewals.set()
            return {"cancel_requested": False}
        if url.endswith("/complete"):
            completed.append(kwargs["body"])
        return {}

    def log(*args):
        blocked.set()
        assert renewals.wait(3), "lease renewal blocked by final log upload"

    monkeypatch.setattr(worker, "ExecutorProcess", exited_process)
    monkeypatch.setattr(worker, "request_json", request)
    monkeypatch.setattr(worker, "post_job_event", log)
    monkeypatch.setattr(worker, "send_heartbeat", lambda *args: None)
    settings = worker.WorkerSettings(workspace_root=tmp_path).model_copy(
        update={"heartbeat_seconds": 0.05}
    )
    worker.execute_job(
        settings,
        {"base_url": "http://test", "api_path": "/api/v1", "node_id": "n", "node_token": "t"},
        {},
        {"test": {"command": [sys.executable, "-c", "print('tail')"]}},
        {"id": "job", "kind": "test", "attempt": 1},
    )
    assert blocked.is_set() and completed[0]["status"] == "succeeded"


@pytest.mark.parametrize("failure", [False, True])
def test_running_cancel_or_lease_failure_cleans_real_child_tree(tmp_path, monkeypatch, failure):
    child_file = tmp_path / "child.pid"
    completed = []
    controls = 0

    def request(url, **kwargs):
        nonlocal controls
        if url.endswith("/control"):
            controls += 1
            if controls > 1:
                deadline = time.monotonic() + 5
                while not child_file.exists() or child_file.stat().st_size == 0:
                    if time.monotonic() > deadline:
                        raise RuntimeError("child did not start")
                    time.sleep(0.02)
                if failure:
                    raise RuntimeError("lease lost")
                return {"cancel_requested": True}
            return {"cancel_requested": False}
        if url.endswith("/complete"):
            completed.append(kwargs["body"])
        return {}

    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
    )
    monkeypatch.setattr(worker, "request_json", request)
    monkeypatch.setattr(worker, "send_heartbeat", lambda *args: None)
    settings = worker.WorkerSettings(workspace_root=tmp_path / "jobs", cancel_grace_seconds=1)

    def run():
        worker.execute_job(
            settings,
            {"base_url": "http://test", "api_path": "/api/v1", "node_id": "n", "node_token": "t"},
            {},
            {"test": {"command": [sys.executable, "-c", parent]}},
            {"id": "job", "kind": "test", "attempt": 1},
        )

    try:
        if failure:
            with pytest.raises(RuntimeError, match="lease lost"):
                run()
            assert not completed
        else:
            run()
            assert completed[0]["status"] == "cancelled"
        pid = int(child_file.read_text())
        try:
            assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        if child_file.exists():
            try:
                psutil.Process(int(child_file.read_text())).kill()
            except psutil.NoSuchProcess:
                pass


def test_lease_renews_while_process_tree_is_terminating(monkeypatch):
    terminating, renewed, terminated = (threading.Event() for _ in range(3))

    def request(*args, **kwargs):
        if terminating.is_set():
            renewed.set()
        return {"cancel_requested": True}

    def terminate(*args):
        terminating.set()
        assert renewed.wait(3), "process termination blocked lease renewal"
        terminated.set()

    monkeypatch.setattr(worker, "request_json", request)
    monkeypatch.setattr(worker, "send_heartbeat", lambda *args: None)
    monkeypatch.setattr(worker, "terminate_process", terminate)
    settings = worker.WorkerSettings().model_copy(update={"heartbeat_seconds": 0.05})
    credentials = {
        "base_url": "http://test",
        "api_path": "/api/v1",
        "node_id": "n",
        "node_token": "t",
    }
    process = SimpleNamespace(stdout=None, close_tree=lambda: None)
    with worker.ExecutionControl(settings, credentials, {}, "job", process) as guard:
        assert terminated.wait(3)
        with pytest.raises(worker.JobCancelled):
            guard.check()
