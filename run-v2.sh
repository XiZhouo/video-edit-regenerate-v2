#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$ROOT/ZE-video-edit-studio"

export PORT="${PORT:-8788}"
export ZE_DATA_DIR="${ZE_DATA_DIR:-$APP_ROOT/.data-v2-local}"
export ZE_SYSTEM_AUTH_DIR="${ZE_SYSTEM_AUTH_DIR:-$ZE_DATA_DIR/auth}"
export ZE_VIDEOGEN_BASE_URL="${ZE_VIDEOGEN_BASE_URL:-http://videogen.tools.hupogames.com}"
export ZE_USERS_PATH="${ZE_USERS_PATH:-$ZE_SYSTEM_AUTH_DIR/users.json}"
export ZE_SESSIONS_PATH="${ZE_SESSIONS_PATH:-$ZE_SYSTEM_AUTH_DIR/sessions.json}"
export ZE_POLICIES_PATH="${ZE_POLICIES_PATH:-$ZE_SYSTEM_AUTH_DIR/policies.json}"
export ZE_AUTH_AUDIT_PATH="${ZE_AUTH_AUDIT_PATH:-$ZE_SYSTEM_AUTH_DIR/audit.jsonl}"
export ZE_NOTIFY_LOG_PATH="${ZE_NOTIFY_LOG_PATH:-$ZE_SYSTEM_AUTH_DIR/notify.jsonl}"

mkdir -p "$ZE_DATA_DIR" "$ZE_SYSTEM_AUTH_DIR"

cd "$APP_ROOT"
exec bash start.sh
