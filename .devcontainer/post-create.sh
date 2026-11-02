#!/usr/bin/env bash
# Install the same uv and Databricks CLI versions as .act/Dockerfile, then the locked environment.
set -euo pipefail

uv_version="0.13.5"
databricks_cli_version="1.22.1"

# PyPI serves the pinned uv release over HTTPS. devcontainer.json puts ~/.local/bin on PATH.
python3 -m pip install --user --no-cache-dir "uv==${uv_version}"
# The Databricks setup script is pinned to its release tag, as in .act/Dockerfile.
curl -fsSL "https://raw.githubusercontent.com/databricks/setup-cli/v${databricks_cli_version}/install.sh" | sudo sh
"$HOME/.local/bin/uv" sync --locked
