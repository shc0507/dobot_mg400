"""Open the MG400 scene and simulation controls without starting robot commands."""

import asyncio
import json
import os
import runpy
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.runtime_paths import data_path  # noqa: E402

REPORT = data_path("isaacsim/runtime.json")


def environment_record():
    """Read source and runtime versions without changing the repository."""
    report = {
        "status": "starting",
        "commit": None,
        "dirty": None,
        "isaac_version": None,
        "python_version": sys.version,
        "scene": str(ROOT / "00_OPEN_MG400.usda"),
        "data_directory": str(REPORT.parent.parent),
        "config_sources": {
            "robot_file": str(ROOT / "config/robot.toml"),
            "environment": {
                name: os.environ.get(name)
                for name in (
                    "ISAACSIM_ROOT",
                    "ISAACSIM_HOST",
                    "ISAACSIM_SIGNAL_PORT",
                    "ISAACSIM_STREAM_PORT",
                    "MG400_DATA_DIR",
                )
            },
        },
    }
    try:
        for field, arguments in (
            ("commit", ["rev-parse", "HEAD"]),
            ("dirty", ["status", "--porcelain"]),
        ):
            output = subprocess.check_output(
                ["git", "-C", str(ROOT), *arguments], text=True, stderr=subprocess.PIPE, timeout=5
            ).strip()
            report[field] = bool(output) if field == "dirty" else output
    except (OSError, subprocess.SubprocessError) as error:
        report["git_error"] = str(error)
    version_path = Path(os.environ.get("ISAACSIM_ROOT", "/isaac-sim")) / "VERSION"
    try:
        report["isaac_version"] = version_path.read_text().strip()
    except OSError as error:
        report["isaac_version_error"] = str(error)
    return report


def validate_control_imports():
    """Check control modules without creating panels, tasks, or network connections."""
    for filename, class_name in (
        ("joint_control.py", "ControlPanel"),
        ("gripper_control.py", "GripperPanel"),
    ):
        namespace = runpy.run_path(
            str(ROOT / "apps/isaacsim" / filename), run_name="mg400_startup_check"
        )
        if class_name not in namespace:
            raise RuntimeError(f"{filename} does not define {class_name}")
    from mg400_agent.gripper_feedback import FileFeedback

    if not callable(FileFeedback):
        raise RuntimeError("FileFeedback is unavailable")


def write_report(report):
    report["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary = REPORT.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(REPORT)


async def startup():
    report = environment_record()
    write_report(report)
    try:
        import omni.kit.app
        import omni.timeline
        import omni.usd

        app = omni.kit.app.get_app()
        context = omni.usd.get_context()
        omni.timeline.get_timeline_interface().stop()
        await app.next_update_async()
        opened, error = await context.open_stage_async(report["scene"])
        if not opened:
            raise RuntimeError(f"Could not open MG400 scene: {error}")
        report["scene"] = context.get_stage().GetRootLayer().realPath
        validate_control_imports()
        report["control_imports"] = "ok"
        runpy.run_path(str(ROOT / "apps/isaacsim/actions.py"))
        report["status"] = "ready"
    except Exception as error:
        report.update(status="error", error=str(error))
        raise
    finally:
        write_report(report)


if __name__ in ("__main__", "<run_path>"):
    MG400_STARTUP_TASK = asyncio.ensure_future(startup())
