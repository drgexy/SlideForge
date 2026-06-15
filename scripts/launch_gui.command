#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! python3 -c "import slideforge" >/dev/null 2>&1; then
  python3 -m pip install -e .
fi

python3 -m slideforge.gui
