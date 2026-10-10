# One image for the shop and the traffic machine. uv is pinned to the version
# `uv --version` reported when this file was written (0.12.7), not a floating tag.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY config ./config
COPY patient ./patient
COPY always_200 ./always_200
COPY traffic ./traffic
# --no-install-project leaves the packages on disk only, so Python must look in /app.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
