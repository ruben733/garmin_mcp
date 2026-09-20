#!/bin/sh
set -eu
umask 077
export GARMINTOKENS="${GARMINTOKENS:-/root/.garminconnect}"
export GARMIN_MCP_PORT="${GARMIN_MCP_PORT:-${PORT:-8000}}"
exec python /app/src/garmin_mcp/token_persistence.py "$@"
