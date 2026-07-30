FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ARG APP_UID=1000
ARG APP_GID=1000

RUN groupadd --gid "${APP_GID}" app \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --home-dir /app \
        --create-home \
        --shell /usr/sbin/nologin \
        app

WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock ./

USER app

RUN uv sync \
    --frozen \
    --no-dev \
    --no-cache

COPY --chown=app:app app/ ./app/
COPY --chown=app:app run.py ./
COPY --chown=app:app config/ ./config/

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "run:app"]
