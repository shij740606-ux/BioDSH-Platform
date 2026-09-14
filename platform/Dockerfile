FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN pip install --no-cache-dir .
CMD ["sh", "-c", "alembic upgrade head && uvicorn biodsh_platform.main:app --host 0.0.0.0 --port 8000"]
