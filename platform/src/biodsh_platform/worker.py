import argparse
import csv
import http.client
import json
import mimetypes
import os
import platform
import queue
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from biodsh_platform import __version__
from biodsh_platform.process_tree import ExecutorProcess, terminate_process
from biodsh_platform.services import job_fits_node


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIODSH_WORKER_", extra="ignore")

    platform_url: str = ""
    node_name: str = ""
    bootstrap_token: SecretStr | None = None
    credential_file: Path = Path.home() / ".config" / "biodsh" / "worker.json"
    metric_path: Path = Path(os.path.abspath(os.sep))
    heartbeat_seconds: int = Field(default=10, ge=5, le=300)
    request_timeout_seconds: int = Field(default=15, ge=3, le=120)
    poll_seconds: int = Field(default=3, ge=1, le=60)
    cancel_grace_seconds: int = Field(default=10, ge=1, le=120)
    executors_file: Path = Path("/etc/biodsh/executors.json")
    workspace_root: Path = Path("/var/lib/biodsh/jobs")


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    merged = {"Accept": "application/json", **(headers or {})}
    if data is not None:
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"platform returned HTTP {exc.code}: {detail}") from exc


def discover(base_url: str, timeout: int) -> tuple[str, str]:
    base = base_url.strip().rstrip("/")
    if not base:
        raise RuntimeError("BIODSH_WORKER_PLATFORM_URL is required for enrollment")
    metadata = request_json(f"{base}/.well-known/biodsh-platform", timeout=timeout)
    api_path = metadata.get("api_versions", {}).get("v1")
    if not isinstance(api_path, str) or not api_path.startswith("/"):
        raise RuntimeError("platform does not advertise a compatible v1 API")
    return base, api_path


def gpu_metrics() -> list[dict[str, Any]]:
    fields = [
        "index",
        "name",
        "uuid",
        "utilization.gpu",
        "memory.used",
        "memory.total",
        "temperature.gpu",
    ]
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    rows: list[dict[str, Any]] = []

    def number(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            return None

    for values in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
        if len(values) != len(fields):
            continue
        rows.append(
            {
                "index": int(values[0]),
                "name": values[1],
                "uuid": values[2],
                "utilization_percent": number(values[3]),
                "memory_used_mib": number(values[4]),
                "memory_total_mib": number(values[5]),
                "temperature_c": number(values[6]),
            }
        )
    return rows


def capabilities() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    return {
        "cpu_logical": psutil.cpu_count(logical=True) or 0,
        "cpu_physical": psutil.cpu_count(logical=False) or 0,
        "memory_total_bytes": memory.total,
        "gpus": [
            {"index": gpu["index"], "name": gpu["name"], "uuid": gpu["uuid"]}
            for gpu in gpu_metrics()
        ],
    }


def collect_metric(metric_path: Path, running_jobs: int = 0) -> dict[str, Any]:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(metric_path))
    network = psutil.net_io_counters()
    try:
        load_one = os.getloadavg()[0]
    except (AttributeError, OSError):
        load_one = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "load_one": load_one,
        "memory_used_bytes": memory.used,
        "memory_total_bytes": memory.total,
        "memory_percent": memory.percent,
        "disk_used_bytes": disk.used,
        "disk_total_bytes": disk.total,
        "disk_percent": disk.percent,
        "network_rx_bytes": network.bytes_recv,
        "network_tx_bytes": network.bytes_sent,
        "running_jobs": running_jobs,
        "gpus": gpu_metrics(),
    }


def save_credentials(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    try:
        temporary_path.chmod(0o600)
    except OSError:
        pass
    temporary_path.replace(path)


def enroll(settings: WorkerSettings) -> None:
    if settings.bootstrap_token is None:
        raise SystemExit("BIODSH_WORKER_BOOTSTRAP_TOKEN is required for enrollment")
    base_url, api_path = discover(settings.platform_url, settings.request_timeout_seconds)
    name = settings.node_name.strip() or socket.gethostname()
    response = request_json(
        f"{base_url}{api_path}/nodes/register",
        method="POST",
        headers={"Authorization": f"Bearer {settings.bootstrap_token.get_secret_value()}"},
        body={
            "name": name,
            "hostname": socket.gethostname(),
            "operating_system": platform.platform(),
            "agent_version": __version__,
            "capabilities": capabilities(),
        },
        timeout=settings.request_timeout_seconds,
    )
    save_credentials(
        settings.credential_file,
        {
            "base_url": base_url,
            "api_path": api_path,
            "node_id": response["node"]["id"],
            "node_token": response["node_token"],
        },
    )
    print(f"Enrolled compute node '{name}' ({response['node']['id']}).")


def load_credentials(settings: WorkerSettings) -> dict[str, str]:
    try:
        credentials = json.loads(settings.credential_file.read_text(encoding="utf-8"))
        return {
            key: str(credentials[key]) for key in ("node_id", "node_token", "base_url", "api_path")
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit(f"invalid worker credential file: {settings.credential_file}") from exc


def load_executors(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid executor configuration: {path}") from exc
    if not isinstance(raw, dict) or not raw:
        raise SystemExit("executor configuration must contain at least one job kind")
    validated: dict[str, dict[str, Any]] = {}
    for kind, profile in raw.items():
        command = profile.get("command") if isinstance(profile, dict) else None
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise SystemExit(f"invalid executor profile: {kind}")
        if not Path(command[0]).is_absolute():
            raise SystemExit(f"executor command must use an absolute path: {kind}")
        validated[kind] = profile
    return validated


def node_headers(credentials: dict[str, str]) -> dict[str, str]:
    return {
        "X-BioDSH-Node-ID": credentials["node_id"],
        "X-BioDSH-Node-Token": credentials["node_token"],
    }


def api_url(credentials: dict[str, str], resource: str) -> str:
    return f"{credentials['base_url']}{credentials['api_path']}{resource}"


def send_heartbeat(
    settings: WorkerSettings,
    credentials: dict[str, str],
    node_capabilities: dict[str, Any],
    running_jobs: int,
) -> None:
    request_json(
        api_url(credentials, f"/nodes/{credentials['node_id']}/heartbeat"),
        method="POST",
        headers=node_headers(credentials),
        body={
            "capabilities": node_capabilities,
            "metric": collect_metric(settings.metric_path, running_jobs),
        },
        timeout=settings.request_timeout_seconds,
    )


def post_job_event(
    settings: WorkerSettings,
    credentials: dict[str, str],
    job_id: str,
    message: str,
) -> None:
    request_json(
        api_url(
            credentials,
            f"/nodes/{credentials['node_id']}/jobs/{job_id}/events",
        ),
        method="POST",
        headers=node_headers(credentials),
        body={"kind": "log", "message": message[:8000], "payload": {}},
        timeout=settings.request_timeout_seconds,
    )


def upload_artifact(
    settings: WorkerSettings,
    credentials: dict[str, str],
    job_id: str,
    relative_path: str,
    source: Path,
    check_control: Callable[[], None] | None = None,
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(credentials["base_url"])
    resource = (
        f"{parsed.path.rstrip('/')}{credentials['api_path']}/nodes/{credentials['node_id']}"
        f"/jobs/{job_id}/artifacts/{urllib.parse.quote(relative_path, safe='/')}"
    )
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(
        parsed.hostname,
        parsed.port,
        timeout=settings.request_timeout_seconds,
    )
    try:
        size = source.stat().st_size
        connection.putrequest("PUT", resource)
        for name, value in node_headers(credentials).items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(size))
        connection.putheader(
            "Content-Type", mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
        )
        connection.endheaders()
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if check_control:
                    check_control()
                connection.send(chunk)
        if check_control:
            check_control()
        response = connection.getresponse()
        payload = response.read()
    finally:
        connection.close()
    if response.status >= 300:
        detail = payload.decode(errors="replace")[:500]
        raise RuntimeError(f"artifact upload returned HTTP {response.status}: {detail}")
    return json.loads(payload)


class JobCancelled(Exception):
    pass


class ExecutionControl:
    """Independent control requests keep leases alive during blocking I/O.

    Each request owns its connection. Metrics run separately so sampling or a
    slow heartbeat cannot prevent lease renewal. No HTTP session is shared.
    """

    def __init__(self, settings, credentials, capabilities, job_id, process):
        self.settings = settings
        self.credentials = credentials
        self.capabilities = capabilities
        self.job_id = job_id
        self.process = process
        self.stopped = threading.Event()
        self.cancelled = threading.Event()
        self.abort = threading.Event()
        self.failure: Exception | None = None
        self.termination_lock = threading.Lock()
        self.tree_terminated = False
        self.threads: list[threading.Thread] = []

    def terminate(self) -> None:
        with self.termination_lock:
            if not self.tree_terminated:
                terminate_process(self.process, self.settings.cancel_grace_seconds)
                self.tree_terminated = True

    def check(self) -> None:
        if self.failure is not None:
            raise RuntimeError(f"job supervision failed: {self.failure}") from self.failure
        if self.cancelled.is_set():
            raise JobCancelled()

    def _control(self) -> None:
        while not self.stopped.is_set():
            control = request_json(
                api_url(
                    self.credentials,
                    f"/nodes/{self.credentials['node_id']}/jobs/{self.job_id}/control",
                ),
                method="POST",
                headers=node_headers(self.credentials),
                body={},
                timeout=self.settings.request_timeout_seconds,
            )
            if control["cancel_requested"] and not self.cancelled.is_set():
                self.cancelled.set()
                self.abort.set()
            if self.stopped.wait(min(self.settings.heartbeat_seconds, 5)):
                return

    def _heartbeat(self) -> None:
        while not self.stopped.is_set():
            send_heartbeat(self.settings, self.credentials, self.capabilities, 1)
            if self.stopped.wait(self.settings.heartbeat_seconds):
                return

    def _terminator(self) -> None:
        self.abort.wait()
        self.terminate()

    def _run(self, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:
            if not self.stopped.is_set():
                self.failure = exc
                self.abort.set()

    def __enter__(self):
        try:
            for action in (self._control, self._heartbeat, self._terminator):
                thread = threading.Thread(target=self._run, args=(action,), daemon=True)
                thread.start()
                self.threads.append(thread)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.abort.set()
        try:
            self.terminate()
        finally:
            for thread in self.threads:
                thread.join()
            self.process.close_tree()
            if self.process.stdout is not None:
                self.process.stdout.close()


def execute_job(
    settings: WorkerSettings,
    credentials: dict[str, str],
    node_capabilities: dict[str, Any],
    executors: dict[str, dict[str, Any]],
    job: dict[str, Any],
) -> None:
    requested = job.get("requested_resources", {})
    if any(requested.values()):
        metric = collect_metric(settings.metric_path)
        if not job_fits_node(
            SimpleNamespace(requested_resources=requested),
            SimpleNamespace(capabilities=node_capabilities),
            SimpleNamespace(**metric),
        ):
            raise RuntimeError("Resources changed after assignment; refusing to start analysis")
        allocated = job.get("payload", {}).get("_allocation", {}).get("gpu_indices", [])
        current = {str(g["index"]): g for g in metric["gpus"]}
        for index in allocated:
            gpu = current.get(index)
            if not gpu or (gpu.get("memory_total_mib") or 0) - (
                gpu.get("memory_used_mib") or 0
            ) < requested.get("gpu_memory_mib", 0):
                raise RuntimeError(
                    "Assigned GPU is no longer available; refusing to start analysis"
                )
    profile = executors[job["kind"]]
    arguments = job.get("payload", {}).get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise RuntimeError("job payload.arguments must be an array of strings")
    max_arguments = int(profile.get("max_arguments", 200))
    if len(arguments) > max_arguments or any(len(value) > 4096 for value in arguments):
        raise RuntimeError("job arguments exceed executor limits")
    job_dir = settings.workspace_root / job["id"] / f"attempt-{job['attempt']}"
    job_dir.mkdir(parents=True, exist_ok=False)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(job_dir),
        "TMPDIR": str(job_dir / "tmp"),
    }
    for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            environment[key] = os.environ[key]
    (job_dir / "tmp").mkdir()
    configured_environment = profile.get("environment", {})
    if not isinstance(configured_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in configured_environment.items()
    ):
        raise RuntimeError("executor environment must contain string keys and values")
    environment.update(configured_environment)
    allocation = job.get("payload", {}).get("_allocation", {})
    if "gpu_indices" in allocation:
        environment["CUDA_VISIBLE_DEVICES"] = ",".join(allocation["gpu_indices"])
    if requested.get("cpu"):
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            environment[key] = str(requested["cpu"])
    command = [*profile["command"], *arguments]
    control = request_json(
        api_url(
            credentials,
            f"/nodes/{credentials['node_id']}/jobs/{job['id']}/control",
        ),
        method="POST",
        headers=node_headers(credentials),
        body={},
        timeout=settings.request_timeout_seconds,
    )
    if control["cancel_requested"]:
        request_json(
            api_url(
                credentials,
                f"/nodes/{credentials['node_id']}/jobs/{job['id']}/complete",
            ),
            method="POST",
            headers=node_headers(credentials),
            body={
                "status": "cancelled",
                "exit_code": None,
                "error_message": "cancelled before process start",
                "result_manifest": {},
            },
            timeout=settings.request_timeout_seconds,
        )
        return
    request_json(
        api_url(
            credentials,
            f"/nodes/{credentials['node_id']}/jobs/{job['id']}/start",
        ),
        method="POST",
        headers=node_headers(credentials),
        body={},
        timeout=settings.request_timeout_seconds,
    )
    process = ExecutorProcess(
        command,
        cwd=job_dir,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
        shell=False,
    )
    output: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            output.put(line.rstrip("\r\n"))
        output.put(None)

    with ExecutionControl(settings, credentials, node_capabilities, job["id"], process) as guard:
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        timeout_seconds = int(profile.get("timeout_seconds", 86_400))
        artifacts: list[dict[str, Any]] = []
        try:
            while process.poll() is None:
                guard.check()
                pending_lines: list[str] = []
                pending_size = 0
                while True:
                    try:
                        line = output.get_nowait()
                    except queue.Empty:
                        break
                    if line:
                        pending_lines.append(line)
                        pending_size += len(line) + 1
                        if pending_size >= 7000:
                            break
                if pending_lines:
                    post_job_event(settings, credentials, job["id"], "\n".join(pending_lines))
                if time.monotonic() - started > timeout_seconds:
                    raise RuntimeError(
                        f"job exceeded executor timeout of {timeout_seconds} seconds"
                    )
                time.sleep(0.2)
            # The leader may have left children holding stdout or writing results.
            guard.terminate()
            reader.join(timeout=5)
            while not output.empty():
                guard.check()
                line = output.get_nowait()
                if line:
                    post_job_event(settings, credentials, job["id"], line)
            guard.check()
            result_globs = profile.get("result_globs", [])
            if not isinstance(result_globs, list) or not all(
                isinstance(value, str) for value in result_globs
            ):
                raise RuntimeError("executor result_globs must be an array of strings")
            seen: set[Path] = set()
            for pattern in result_globs:
                for result in job_dir.glob(pattern):
                    guard.check()
                    if result.is_file() and result not in seen:
                        resolved = result.resolve()
                        if job_dir.resolve() not in resolved.parents:
                            raise RuntimeError(f"result file escapes the job workspace: {result}")
                        seen.add(result)
                        relative = result.relative_to(job_dir).as_posix()
                        artifacts.append(
                            upload_artifact(
                                settings, credentials, job["id"], relative, result, guard.check
                            )
                        )
            guard.check()
            final_status = "succeeded" if process.returncode == 0 else "failed"
        except JobCancelled:
            guard.terminate()
            final_status = "cancelled"
        request_json(
            api_url(credentials, f"/nodes/{credentials['node_id']}/jobs/{job['id']}/complete"),
            method="POST",
            headers=node_headers(credentials),
            body={
                "status": final_status,
                "exit_code": process.returncode,
                "error_message": ""
                if final_status == "succeeded"
                else "cancelled by request"
                if final_status == "cancelled"
                else f"process exited with code {process.returncode}",
                "result_manifest": {"artifacts": artifacts},
            },
            timeout=settings.request_timeout_seconds,
        )


def heartbeat(settings: WorkerSettings, *, once: bool = False) -> None:
    credentials = load_credentials(settings)
    executors = load_executors(settings.executors_file)
    headers = {
        "X-BioDSH-Node-ID": credentials["node_id"],
        "X-BioDSH-Node-Token": credentials["node_token"],
    }
    node_capabilities = capabilities()
    node_capabilities["executors"] = sorted(executors)
    retry_seconds = 1
    while True:
        started = time.monotonic()
        try:
            send_heartbeat(settings, credentials, node_capabilities, 0)
            job = request_json(
                api_url(credentials, f"/nodes/{credentials['node_id']}/jobs/claim"),
                method="POST",
                headers=headers,
                body={"supported_kinds": sorted(executors)},
                timeout=settings.request_timeout_seconds,
            )
            if job is not None:
                try:
                    execute_job(settings, credentials, node_capabilities, executors, job)
                except Exception as exc:
                    try:
                        request_json(
                            api_url(
                                credentials,
                                f"/nodes/{credentials['node_id']}/jobs/{job['id']}/complete",
                            ),
                            method="POST",
                            headers=headers,
                            body={
                                "status": "failed",
                                "exit_code": None,
                                "error_message": str(exc)[:4000],
                                "result_manifest": {},
                            },
                            timeout=settings.request_timeout_seconds,
                        )
                    except (OSError, RuntimeError):
                        pass
                    raise
            retry_seconds = 1
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} worker cycle completed")
        except Exception as exc:
            if once:
                raise SystemExit(f"worker cycle failed: {exc}") from exc
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} worker cycle failed: {exc}")
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 60)
            continue
        if once:
            return
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, settings.poll_seconds - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser(prog="biodsh-worker")
    parser.add_argument("command", choices=["enroll", "run"])
    parser.add_argument("--once", action="store_true", help="send one heartbeat and exit")
    args = parser.parse_args()
    settings = WorkerSettings()
    if args.command == "enroll":
        enroll(settings)
    else:
        heartbeat(settings, once=args.once)


if __name__ == "__main__":
    main()
