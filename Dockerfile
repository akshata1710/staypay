# One image for the API, the workers and MockPay; the command decides which runs.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" "sqlalchemy[asyncio]>=2.0.30" \
    "asyncpg>=0.29" "alembic>=1.13" "redis>=5.0" "pydantic>=2.7" "pydantic-settings>=2.3" "pyyaml>=6.0" \
    "httpx>=0.27" "prometheus-client>=0.20"

COPY alembic.ini ./
COPY app ./app
COPY mockpay ./mockpay
COPY policies ./policies

RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
