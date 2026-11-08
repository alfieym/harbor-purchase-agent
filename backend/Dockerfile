# One image, two processes. The API and the S15 worker run the same code with
# the same data paths, so a turn cannot behave differently depending on which
# container answered it; docker-compose.yml only changes the command.
#
# Build from the repository root:
#   docker build -f backend/Dockerfile .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first: editing a source file must not reinstall the world.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ``uv sync`` installs the project in editable mode, so ``__file__`` stays
# under /app/src — which is how harbor derives its data and cache paths
# (``parents[4]``). Do not switch to ``--no-editable``; it would silently move
# SQLite and the index fingerprint into site-packages.
COPY backend/src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8010
CMD ["uvicorn", "harbor.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8010"]
