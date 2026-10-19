"""Open the combined arm and gripper panel through the legacy Olares entry."""

import runpy
from pathlib import Path


def start():
    return runpy.run_path(
        str(Path(__file__).with_name("joint_control.py")),
    )


if __name__ in ("__main__", "<run_path>"):
    start()
