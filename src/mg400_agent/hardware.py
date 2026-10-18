"""MG400 adapter extension point. No socket connections or automatic robot enable."""

from .backends import Arm, Gripper


def connect_mg400(host: str) -> tuple[Arm, Gripper]:
    """Implement using the matching 4-axis SDK and the actual electric gripper protocol.

    Arm.move_linear must wait for verified completion, enforce a timeout, and report
    controller/feedback faults. Gripper.grasp must inspect object-detected feedback.
    Keep gripper transport separate: electric grippers may use I/O or Modbus.
    See docs/hardware.md before implementing this boundary.
    """
    raise NotImplementedError(
        "MG400 hardware adapter is not implemented. Confirm firmware, calibrated poses, "
        "and electric gripper model/protocol first; use --backend sim for this scaffold."
    )
