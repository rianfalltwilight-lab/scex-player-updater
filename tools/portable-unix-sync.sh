#!/bin/sh
# Run inside the distributed client instance; no launcher is copied or started.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTANCE_DIR=${PORTABLE_INSTANCE_DIR:-$SCRIPT_DIR}
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3.10+ is required.' >&2
    exit 2
fi
if [ ! -f "$INSTANCE_DIR/_updater/player-update-generic.py" ]; then
    echo 'Place this entry inside the client instance containing _updater.' >&2
    exit 2
fi
exec python3 "$INSTANCE_DIR/_updater/player-update-generic.py" --instance-dir "$INSTANCE_DIR" "$@"
