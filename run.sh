#!/usr/bin/env bash
set -e
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x ".venv/bin/python" ]; then
  exec .venv/bin/python launch.py
fi
exec "${PYTHON:-python3}" launch.py
