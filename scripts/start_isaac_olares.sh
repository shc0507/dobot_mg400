#!/bin/sh
# Start the installed Isaac Sim service with the MG400 simulation scene.
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)

export ISAACSIM_ROOT="${ISAACSIM_ROOT:-/isaac-sim}"
export ISAACSIM_HOST="${ISAACSIM_HOST:-100.64.0.1}"
export ISAACSIM_SIGNAL_PORT="${ISAACSIM_SIGNAL_PORT:-49100}"
export ISAACSIM_STREAM_PORT="${ISAACSIM_STREAM_PORT:-47998}"
export MG400_DATA_DIR="${MG400_DATA_DIR:-/root/Documents/mg400-data}"

default_startup=true
for argument in "$@"; do
    case "$argument" in
        --exec|--exec=*) default_startup=false ;;
    esac
done
if [ "$default_startup" = true ]; then
    set -- --exec "$repo_root/apps/isaacsim/startup.py" "$@"
fi

cd /workspace/isaaclab
exec "$ISAACSIM_ROOT/runheadless.sh" \
    --/exts/isaacsim.core.throttling/enable_async=false \
    --/app/asyncRendering=false \
    --/app/asyncRenderingLowLatency=false "$@"
