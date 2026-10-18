"""Open the Olares gripper panel with feedback from the shared data directory."""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.runtime_paths import data_path  # noqa: E402


def start():
    return runpy.run_path(
        str(Path(__file__).with_name("gripper_control.py")),
        init_globals={"FEEDBACK_PATH": str(data_path("work/gripper_olares_feedback.json"))},
    )


if __name__ in ("__main__", "<run_path>"):
    start()
