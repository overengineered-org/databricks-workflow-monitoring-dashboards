FROM catthehacker/ubuntu:act-latest

LABEL org.opencontainers.image.title="Databricks Workflow Dashboards local act runner"
LABEL org.opencontainers.image.description="Repository-specific local runner for act validation"

WORKDIR /workspace

# uv installs the locked Python environment. Its official image supports ARM64.
COPY --from=ghcr.io/astral-sh/uv:0.12.2 /uv /uvx /usr/local/bin/
COPY --from=golang:1.26.6-bookworm /usr/local/go /usr/local/go
COPY --from=golangci/golangci-lint:v2.12.2 /usr/bin/golangci-lint /usr/local/bin/

# act mounts the repository on another filesystem, so copy dependencies into .venv.
ENV UV_LINK_MODE=copy
ENV PATH="/usr/local/go/bin:${PATH}"

# Pin the Databricks CLI so local validation does not change unexpectedly.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends jq python3 python3-venv ripgrep \
    && curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/v1.13.0/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*
