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


# The provisioner: the same code started differently (python -m
# gateway.provisioner), plus what it needs to create stores and run hosted MCP
# servers: the helm command and the two charts. It is a separate image so that
# the API, which faces the internet, carries none of it. Built with
#   --target provisioner --build-context store-chart=charts/store
#                        --build-context server-chart=charts/mcp-server
FROM python:3.14-slim AS provisioner

COPY --from=alpine/helm:4.3.0 /usr/bin/helm /usr/local/bin/helm
COPY --from=store-chart . /charts/store
COPY --from=server-chart . /charts/mcp-server
# The charts are read at run time by an unprivileged user. Their permissions
# are set here so they do not depend on the checkout they were copied from.
RUN chmod -R a+rX /charts
COPY --from=build /app/.venv /app/.venv
# The root filesystem is read-only at run time; helm keeps its files in /tmp.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    HELM_CACHE_HOME=/tmp/helm/cache \
    HELM_CONFIG_HOME=/tmp/helm/config \
    HELM_DATA_HOME=/tmp/helm/data

USER 10001:10001
ENTRYPOINT ["python", "-m", "gateway.provisioner"]


# The API. Last in the file, so it is what a plain `docker build` produces.
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
