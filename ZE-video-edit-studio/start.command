#!/usr/bin/env bash
# macOS 双击入口；本机模式只绑定 127.0.0.1，不调用生产部署脚本。
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

PYTHON=""
for CANDIDATE in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
  if command -v "$CANDIDATE" >/dev/null 2>&1 \
    && "$CANDIDATE" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 14) else 1)' >/dev/null 2>&1; then
    PYTHON="$CANDIDATE"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "[ZE][FATAL] 未找到 Python 3.9–3.13：https://www.python.org/downloads/" >&2
  read -r -p "按回车关闭…" _
  exit 1
fi

"$PYTHON" scripts/local_launcher.py
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
  echo
  read -r -p "启动失败；按回车关闭…" _
fi
exit "$STATUS"
