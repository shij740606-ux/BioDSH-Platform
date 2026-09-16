#!/usr/bin/env python3
"""Validate Feishu credentials and enable Feishu integration safely."""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def request_json(url: str, *, body: dict[str, str] | None = None, token: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=15
        ) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Feishu request failed: {exc}") from exc


def values(text: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def replace_values(text: str, replacements: dict[str, str]) -> str:
    pending = replacements.copy()
    lines: list[str] = []
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in pending:
            lines.append(f"{key}={pending.pop(key)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(lines) + "\n"


def write_private(text: str) -> None:
    fd, name = tempfile.mkstemp(dir=ROOT, prefix=".env.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(name, ENV_FILE)
        os.chmod(ENV_FILE, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "--env-file", ".env", *args], cwd=ROOT, check=True
    )


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo.")
    if not ENV_FILE.is_file():
        raise SystemExit(f"Missing {ENV_FILE}; run bootstrap.sh first.")

    original = ENV_FILE.read_text(encoding="utf-8")
    app_id = values(original).get("BIODSH_FEISHU_APP_ID", "").strip()
    if not app_id:
        raise SystemExit("BIODSH_FEISHU_APP_ID is missing from .env")
    secret = getpass.getpass("Feishu App Secret: ").strip()
    if not secret or any(char in secret for char in "\r\n"):
        raise SystemExit("App Secret must not be empty.")

    token_result = request_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
        body={"app_id": app_id, "app_secret": secret},
    )
    if token_result.get("code") != 0 or not token_result.get("tenant_access_token"):
        raise SystemExit(f"Feishu rejected the credentials: {token_result.get('msg', 'unknown error')}")

    tenant_result = request_json(
        "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
        token=token_result["tenant_access_token"],
    )
    tenant = tenant_result.get("data", {}).get("tenant", {})
    tenant_key = tenant.get("tenant_key", "")
    if tenant_result.get("code") != 0 or not tenant_key:
        raise SystemExit(
            "Could not read tenant_key. Add the Feishu permission for reading enterprise "
            f"information, then retry. Feishu said: {tenant_result.get('msg', 'unknown error')}"
        )

    updated = replace_values(
        original,
        {
            "BIODSH_FEISHU_APP_SECRET": secret,
            "BIODSH_FEISHU_ALLOWED_TENANTS": json.dumps([tenant_key]),
            "BIODSH_FEISHU_LOGIN_ENABLED": "true",
            "BIODSH_FEISHU_NOTIFICATIONS_ENABLED": "true",
        },
    )
    write_private(updated)
    try:
        compose("config", "--quiet")
        compose("up", "-d", "platform")
    except subprocess.CalledProcessError:
        write_private(original)
        compose("up", "-d", "platform")
        raise SystemExit("Configuration failed; the previous .env was restored.")

    print(f"Feishu enabled for {tenant.get('name', 'tenant')} ({tenant_key}).")
    print("Callback: " + values(updated)["BIODSH_FEISHU_REDIRECT_URI"])


if __name__ == "__main__":
    main()
