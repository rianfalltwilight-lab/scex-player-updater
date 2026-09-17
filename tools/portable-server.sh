#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3.10+ is required. Install your distribution python3 package.' >&2
    exit 2
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 2)' || {
    echo 'Python 3.10+ is required.' >&2
    exit 2
}
exec python3 "$SCRIPT_DIR/portable_server.py" "$@"
