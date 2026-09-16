#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo." >&2
    exit 1
fi
: "${BIODSH_DOMAIN:?set BIODSH_DOMAIN}"
: "${BIODSH_FEISHU_APP_ID:?set BIODSH_FEISHU_APP_ID}"

cd "$(dirname "$0")"
if [ ! -f .env ]; then
    umask 077
    postgres_password="$(openssl rand -hex 24)"
    platform_token="$(openssl rand -hex 32)"
    jwt_secret="$(openssl rand -hex 48)"
    cat > .env <<EOF
CADDY_IMAGE=caddy:2.10.2-alpine
POSTGRES_IMAGE=postgres:16.10-alpine
POSTGRES_DB=biodsh
POSTGRES_USER=biodsh
POSTGRES_PASSWORD=${postgres_password}
BIODSH_DOMAIN=${BIODSH_DOMAIN}
BIODSH_SERVICE_NAME=BioDSH Platform
BIODSH_CONTAINER_PORT=8000
BIODSH_LOG_LEVEL=info
BIODSH_API_PREFIX=/api/v1
BIODSH_DOCS_ENABLED=false
BIODSH_DATABASE_URL=postgresql+psycopg://biodsh:${postgres_password}@postgres:5432/biodsh
BIODSH_DATABASE_POOL_SIZE=5
BIODSH_DATABASE_MAX_OVERFLOW=5
BIODSH_PLATFORM_TOKEN=${platform_token}
BIODSH_JWT_SECRET=${jwt_secret}
BIODSH_JWT_ISSUER=biodsh-platform
BIODSH_JWT_AUDIENCE=biodsh-desktop
BIODSH_ACCESS_TOKEN_MINUTES=15
BIODSH_REFRESH_TOKEN_DAYS=30
BIODSH_LOGIN_MAX_ATTEMPTS=5
BIODSH_LOGIN_LOCK_MINUTES=15
BIODSH_NODE_OFFLINE_SECONDS=45
BIODSH_METRICS_RETENTION_DAYS=7
BIODSH_JOB_LEASE_SECONDS=45
BIODSH_ARTIFACT_MAX_BYTES=1073741824
BIODSH_CORS_ORIGINS=[]
BIODSH_PROJECT_NODE_AUTHORIZATION_ENABLED=true
BIODSH_PUBLIC_BASE_URL=https://${BIODSH_DOMAIN}
BIODSH_FEISHU_APP_ID=${BIODSH_FEISHU_APP_ID}
BIODSH_FEISHU_APP_SECRET=
BIODSH_FEISHU_REDIRECT_URI=https://${BIODSH_DOMAIN}/api/v1/auth/feishu/callback
BIODSH_FEISHU_ALLOWED_TENANTS=[]
BIODSH_FEISHU_LOGIN_ENABLED=false
BIODSH_FEISHU_NOTIFICATIONS_ENABLED=false
EOF
fi
chmod 600 .env

docker compose --env-file .env config --quiet
docker compose --env-file .env pull postgres gateway
docker compose --env-file .env build platform
docker compose --env-file .env up -d
