# Source: Chemistry-Integration/Digital_Twin/src/furnace_twin/marker_registry.py
"""Physical ArUco attachments used by calibration and the visualizer.

Edit assignments here when stickers are added or remounted.
Membership does not establish a calibrated position:
each saved calibration contains the marker pairs actually fitted from photos.
"""

from dataclasses import dataclass

ARUCO_DICTIONARY = "DICT_4X4_50"


@dataclass(frozen=True)
class MarkerGroup:
    """Markers rigidly attached to one bench component."""

    label: str
    attached_to: str
    marker_ids: tuple[int, ...]
    parent: str | None


# Desk -> lower Y stage (Layer A) -> upper X stage (Layer B) -> insert.
# A marker ID identifies one physical sticker; do not reuse an ID elsewhere.
MARKER_GROUPS = {
    "desk": MarkerGroup(
        label="Desk",
        attached_to="Stationary desk reference surface",
        marker_ids=(10, 11, 12),
        parent=None,
    ),
    "y": MarkerGroup(
        label="Y stage",
        attached_to="Bottom moving Y stage, Layer A (up/down)",
        marker_ids=(14, 15, 16),
        parent="desk",
    ),
    "x": MarkerGroup(
        label="X stage",
        attached_to="Top moving X stage, Layer B (left/right)",
        marker_ids=(18, 19, 20),
        parent="y",
    ),
    "insert": MarkerGroup(
        label="Sample insert",
        attached_to="Moving insert carriage (into/out of the furnace)",
        marker_ids=(22,),
        parent="x",
    ),
}
