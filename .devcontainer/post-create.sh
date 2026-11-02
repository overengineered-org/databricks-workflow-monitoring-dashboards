#!/usr/bin/env bash
# Install the same uv and Databricks CLI versions as .act/Dockerfile, then the locked environment.
set -euo pipefail

uv_version="0.13.5"
databricks_cli_version="1.22.1"

curl -LsSf "https://astral.sh/uv/${uv_version}/install.sh" | sh
curl -fsSL "https://raw.githubusercontent.com/databricks/setup-cli/v${databricks_cli_version}/install.sh" | sudo sh
"$HOME/.local/bin/uv" sync --locked
