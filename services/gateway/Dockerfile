# The gateway: REST API for the dashboard, the MCP endpoint, and the chat agent.
#
# Two stages: the first installs the locked dependencies into a virtualenv with
# uv, the second copies only that virtualenv onto a clean Python image. The
# final image has no build tools, no package manager cache and no source tree.
FROM python:3.14-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /usr/local/bin/uv
# Use the image's own Python, copy files instead of linking them (the
# virtualenv has to survive being copied to the next stage), and compile to
# bytecode now because the runtime filesystem is read-only.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
# Dependencies first: this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.14-slim

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A numeric, unprivileged user: Kubernetes can only verify runAsNonRoot against
# a number, not a name.
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "gateway"]
