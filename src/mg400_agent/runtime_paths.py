"""Keep runtime files outside the versioned scene assets."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def data_path(relative_path: str) -> Path:
    """Return an absolute runtime path and create its parent directories."""
    directory = Path(os.environ.get("MG400_DATA_DIR") or ROOT / "work").expanduser().resolve()
    path = directory / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export_scene(stage, output_path: str | Path) -> bool:
    """Export local layer edits while preserving composition arcs and source asset paths."""
    from pxr import UsdUtils

    local_layers = set(stage.GetLayerStack(includeSessionLayers=True))
    unsaved = sorted(
        layer.identifier
        for layer in stage.GetUsedLayers()
        if layer not in local_layers and layer.dirty
    )
    if unsaved:
        raise RuntimeError(
            "Save modified referenced assets before exporting: " + ", ".join(unsaved)
        )
    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    layer = UsdUtils.FlattenLayerStack(stage)
    return layer.Export(str(path))
