# BioDSH Platform

This directory contains the BioDSH multi-user and multi-server control plane.
Milestone 1 provides a FastAPI service, PostgreSQL models and migrations, projects,
compute-node registration, a persistent job queue, job events, and API tests.

## Local development

```powershell
uv sync --extra dev
$env:BIODSH_DATABASE_URL = "sqlite:///./biodsh-dev.db"
$env:BIODSH_PLATFORM_TOKEN = "local-development-token"
$env:BIODSH_AUTO_CREATE_SCHEMA = "true"
$env:BIODSH_API_PREFIX = "/api/v1"
uv run biodsh-platform
```

The health endpoint is public. Other endpoints require:

```text
Authorization: Bearer local-development-token
```

Validation:

```powershell
uv run ruff check .
uv run pytest -q
```

For PostgreSQL, copy `.env.example` to `.env`, replace the token, and run
`docker compose -f deploy/compose.yaml up -d --build` from the repository root.

The static bearer token is a milestone-1 bootstrap bridge. It is externally configured,
validated at startup, and must never be used as end-user authentication. User login,
revocable sessions, operating-system credential storage, and project RBAC replace it in
milestone 2 before the product is exposed to users.

All production deployments must follow `docs/engineering-standards.zh.md`. Production
configuration has no database or token fallback, uses Alembic migrations, and exposes the
versioned API under `/api/v1` by default.
