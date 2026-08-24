#!/usr/bin/env bash
# RaceEngine V2 — arranque en macOS / Linux
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-}
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys;exit(0 if sys.version_info>=(3,12) else 1)' 2>/dev/null; then
      PY=$c; break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "──────────────────────────────────────────────────────"
  echo "  No se encontró Python 3.12 o superior."
  echo ""
  echo "  macOS:  brew install python@3.12"
  echo "          (o el instalador de https://www.python.org/downloads/macos/)"
  echo "  Linux:  sudo apt install python3.12 python3.12-venv"
  echo "──────────────────────────────────────────────────────"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "▸ creando entorno virtual con $PY…"
  "$PY" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f web/vendor/leaflet.js ]; then
  echo "▸ descargando Leaflet…"
  ./vendor.sh 2>/dev/null || echo "  (sin red: se usará el CDN al abrir la app)"
fi

exec ./.venv/bin/python serve.py "$@"
