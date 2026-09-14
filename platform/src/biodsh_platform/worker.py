import argparse
import csv
import json
import os
import platform
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from biodsh_platform import __version__


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIODSH_WORKER_", extra="ignore")

    platform_url: str = ""
    node_name: str = ""
    bootstrap_token: SecretStr | None = None
    credential_file: Path = Path.home() / ".config" / "biodsh" / "worker.json"
    metric_path: Path = Path(os.path.abspath(os.sep))
    heartbeat_seconds: int = Field(default=10, ge=5, le=300)
    request_timeout_seconds: int = Field(default=15, ge=3, le=120)


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
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


def collect_metric(metric_path: Path) -> dict[str, Any]:
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
        "running_jobs": 0,
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


def heartbeat(settings: WorkerSettings, *, once: bool = False) -> None:
    try:
        credentials = json.loads(settings.credential_file.read_text(encoding="utf-8"))
        node_id = credentials["node_id"]
        node_token = credentials["node_token"]
        base_url = credentials["base_url"]
        api_path = credentials["api_path"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SystemExit(f"invalid worker credential file: {settings.credential_file}") from exc
    headers = {
        "X-BioDSH-Node-ID": node_id,
        "X-BioDSH-Node-Token": node_token,
    }
    url = f"{base_url}{api_path}/nodes/{node_id}/heartbeat"
    node_capabilities = capabilities()
    retry_seconds = 1
    while True:
        started = time.monotonic()
        try:
            request_json(
                url,
                method="POST",
                headers=headers,
                body={
                    "capabilities": node_capabilities,
                    "metric": collect_metric(settings.metric_path),
                },
                timeout=settings.request_timeout_seconds,
            )
            retry_seconds = 1
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} heartbeat sent")
        except (OSError, RuntimeError) as exc:
            if once:
                raise SystemExit(f"heartbeat failed: {exc}") from exc
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} heartbeat failed: {exc}")
            time.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 60)
            continue
        if once:
            return
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, settings.heartbeat_seconds - elapsed))


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
