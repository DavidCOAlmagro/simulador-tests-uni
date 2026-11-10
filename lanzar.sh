#!/bin/sh
# Lanza el test con el Python del venv de esta carpeta (.venv).
# Si no existe, prueba con el entorno micromamba "ice" y, si no, con el python3 del sistema.
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$HOME/micromamba/envs/ice/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" "$DIR/test_app.py" "$@"
