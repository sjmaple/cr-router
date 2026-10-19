#!/usr/bin/env bash
set -euo pipefail
bridge_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --script "$bridge_root/bridge.py" --tui "$@"
