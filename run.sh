#!/usr/bin/env bash
# Interactive launcher for ahrefs_checker.py
# Lets you pick which mode(s) to run.

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV_PY=""
if [[ -x ".venv/bin/python3" ]]; then
    VENV_PY=".venv/bin/python3"
elif [[ -x "venv/bin/python3" ]]; then
    VENV_PY="venv/bin/python3"
else
    VENV_PY="$(command -v python3)"
fi

echo "╔════════════════════════════════════════╗"
echo "║     Ahrefs Local Checker Launcher      ║"
echo "╠════════════════════════════════════════╣"
echo "║  1) Authority only (DR, backlinks)     ║"
echo "║  2) Traffic only (organic traffic)     ║"
echo "║  3) Both (authority + traffic)         ║"
echo "╚════════════════════════════════════════╝"
echo ""
read -rp "Select mode [1/2/3] (default: 3): " choice

case "${choice:-3}" in
    1) MODES="authority" ;;
    2) MODES="traffic" ;;
    3) MODES="authority,traffic" ;;
    *) echo "Invalid choice. Using both."; MODES="authority,traffic" ;;
esac

read -rp "Number of workers (default: 3): " workers
workers="${workers:-3}"

echo ""
echo "[*] Starting ahrefs_checker.py --modes ${MODES} --workers ${workers} --no-proxy"
echo ""

exec "$VENV_PY" ahrefs_checker.py --modes "${MODES}" --workers "${workers}" --no-proxy "$@"
