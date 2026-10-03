# BioDSH Platform

BioDSH Platform is the FastAPI/PostgreSQL monolith used by the desktop client and compute nodes. It owns users, projects, project membership, nodes, jobs, refresh sessions, and audit records.

## Configuration

Copy `.env.example` to `.env` for development and replace every placeholder. Production deployments must inject independent random values for `BIODSH_PLATFORM_TOKEN` and `BIODSH_JWT_SECRET`; neither value has a source-code fallback.

```powershell
uv sync --extra dev
$env:BIODSH_DATABASE_URL = "sqlite:///./biodsh-dev.db"
$env:BIODSH_PLATFORM_TOKEN = "replace-with-a-long-random-bootstrap-token"
$env:BIODSH_JWT_SECRET = "replace-with-an-independent-32-character-random-secret"
$env:BIODSH_AUTO_CREATE_SCHEMA = "true"
uv run biodsh-platform
```

Production uses PostgreSQL and Alembic:

```powershell
uv run alembic upgrade head
uv run biodsh-platform init-admin --username admin
uv run biodsh-platform
```

`init-admin` prompts for the password without accepting it as a command-line argument. The equivalent `POST /api/v1/bootstrap/admin` endpoint requires the static bootstrap token and permanently refuses initialization after the first administrator exists.

## Authentication and authorization

- End users log in through `POST /api/v1/auth/login` with a username and password.
- Passwords use Argon2id. Successful login upgrades an old Argon2 parameter set when needed.
- Repeated failed logins trigger a configurable database-backed account lock without exposing whether a username exists.
- Access tokens expire after `BIODSH_ACCESS_TOKEN_MINUTES` and validate signature, algorithm, issuer, audience, expiry, token type, and token version.
- Refresh tokens are random opaque values. Only their SHA-256 digest is stored; every refresh rotates and revokes the previous token.
- Administrators can revoke every active token for a user. This invalidates existing access tokens through the user's token version.
- The static platform token is accepted only by first-administrator initialization and one-time node registration. Heartbeats and job execution use the node's separate credential.

System administrators manage users and nodes. Project owners manage membership, analysts submit and cancel jobs, and viewers read projects, jobs, events, and project audit records. The API checks membership for every resource ID and hides cross-project resources with `404`.

The Tauri desktop stores the platform session in the operating system keyring. On Windows this is Windows Credential Manager. `settings.json` contains only the platform URL and selected project ID; old `platformToken` values are removed during settings migration.

For a production Feishu setup, first add the callback URL shown below to the custom app's security settings and grant the app permission to read enterprise information. Then run the interactive helper on the server. It validates the App Secret without printing it, discovers the tenant key, updates the restricted `.env`, and recreates the platform container.

```bash
https://your-platform.example.org/api/v1/auth/feishu/callback
sudo python3 deploy/configure-feishu.py
```

## Compute workers and resource metrics

Each compute server runs `biodsh-worker`. Enrollment uses the bootstrap token once and returns a unique node credential; the database stores only its SHA-256 digest. Heartbeats use the per-node credential and report CPU, memory, disk, network, NVIDIA GPU, and running-job metrics. The desktop reads the consolidated node list and history through the Platform API, so adding servers does not add desktop credentials or direct SSH connections.

Workers advertise locally configured executor kinds, atomically claim jobs that fit current CPU, memory, disk, and GPU capacity, renew execution leases, stream logs, process cancellation, and upload configured result files. Commands are fixed in the server-side executor registry and launched without a shell in an isolated job directory.

```powershell
$env:BIODSH_WORKER_PLATFORM_URL = "https://platform.example.org"
$env:BIODSH_WORKER_NODE_NAME = "compute-01"
$env:BIODSH_WORKER_BOOTSTRAP_TOKEN = "replace-with-platform-bootstrap-token"
$env:BIODSH_WORKER_EXECUTORS_FILE = "C:\BioDSH\executors.json"
$env:BIODSH_WORKER_WORKSPACE_ROOT = "C:\BioDSH\jobs"
uv run biodsh-worker enroll
Remove-Item Env:BIODSH_WORKER_BOOTSTRAP_TOKEN
uv run biodsh-worker run --once
uv run biodsh-worker run
```

The Linux systemd deployment and multi-server connection model are documented in `docs/server-worker.zh.md`.

## Validation

```powershell
uv run ruff check .
uv run pytest -q
uv run alembic upgrade head
uv run alembic downgrade base
```

Container dependencies are generated from the lock file:

```powershell
uv export --frozen --no-dev --no-hashes --no-emit-project --output-file requirements.txt
```

All production work follows `docs/engineering-standards.zh.md`. The milestone is ready for staging only after migrations, desktop login, token storage, and cross-project checks pass against the staging PostgreSQL deployment.

### Worker lifecycle regression checks

Lease renewal runs independently of process output, metrics, result uploads, and
process-tree termination. Cancellation stops further result transfers; a blocked
socket operation remains bounded by `BIODSH_WORKER_REQUEST_TIMEOUT_SECONDS`.
Windows executors start suspended, enter a kill-on-close Job Object, then resume;
failure to establish this containment fails execution. POSIX executors use a new
process group and escalate termination for the whole group even when its leader
has already exited. Executors must not deliberately detach into another session.

Task state changes, lease renewal/recovery and event sequence allocation use one
transactional job write lock (row-level on PostgreSQL, database-level on SQLite).
Artifact streaming holds no job write lock and revalidates state before publishing
the artifact. No schema migration or dependency change is required.

The normal test suite includes concurrent SQLite transactions, real process-tree
cleanup, and a slow local HTTP upload with cancellation. PostgreSQL concurrency
checks run when `BIODSH_TEST_POSTGRES_URL` points to a **dedicated test database**;
they create and drop an isolated `biodsh_test_*` schema for each test. Without that
variable they are explicitly skipped. CI provides PostgreSQL 16 and tests the
Worker separately on Windows; the Linux job covers POSIX process groups.

```powershell
$env:BIODSH_TEST_POSTGRES_URL = "postgresql+psycopg://test:test@127.0.0.1:5432/biodsh_test"
uv run pytest -q tests/test_job_concurrency.py
uv run pytest -q tests/test_worker.py tests/test_worker_supervision.py tests/test_process_tree.py
```
