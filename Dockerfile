FROM --platform=$BUILDPLATFORM node:26-slim AS web
WORKDIR /app/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
COPY frontend/tools/codegen/package.json tools/codegen/
RUN npm install --global "$(node -p "require('./package.json').packageManager")"
RUN --mount=type=cache,id=pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --store-dir /pnpm-store
COPY frontend/ ./
RUN pnpm build

FROM --platform=$BUILDPLATFORM python:3.14-slim AS wheel
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/.python-version ./
COPY backend/src ./src
COPY --from=web /app/backend/src/alpha_harness/web ./src/alpha_harness/web
ARG VERSION=0.0.0.dev0
RUN uv version --frozen "$VERSION" && uv build --wheel --out-dir /dist

FROM python:3.14-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.14-slim
RUN useradd --create-home --uid 1000 harness && chmod 755 /home/harness
ENV HOME=/home/harness PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=deps /app/.venv /app/.venv
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL fts')"
RUN --mount=from=wheel,source=/dist,target=/dist \
    --mount=from=ghcr.io/astral-sh/uv:0.12.18,source=/uv,target=/usr/local/bin/uv \
    uv pip install --no-cache --python /app/.venv/bin/python --no-deps /dist/*.whl \
    && python -m compileall -q /app/.venv/lib/python3.14/site-packages/alpha_harness
USER harness
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=2m \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
CMD ["uvicorn", "alpha_harness.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
