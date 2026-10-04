#!/usr/bin/env bash
# One-command on-sale stampede against a deployment, with a full correctness report.
#
#   ./scripts/burst.sh <BASE_URL> [ADMIN_KEY] [extra flags, e.g. --requests 5000]
#
# ADMIN_KEY defaults to $ADMIN_KEY, then to the local development key.
# Needs uv (https://docs.astral.sh/uv/); without it, falls back to Docker.
set -euo pipefail

BASE=${1:?usage: scripts/burst.sh BASE_URL [ADMIN_KEY] [flags]}
shift
KEY=${ADMIN_KEY:-local-dev-admin-key}
if [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; then KEY=$1; shift; fi
HERE=$(cd "$(dirname "$0")" && pwd)

if command -v uv > /dev/null; then
  exec uv run --quiet --script "$HERE/burst.py" "$BASE" --admin-key "$KEY" "$@"
fi
exec docker run --rm --network host -v "$HERE:/scripts:ro" \
  ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
  uv run --quiet --script /scripts/burst.py "$BASE" --admin-key "$KEY" "$@"
