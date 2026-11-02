"""Filesystem helpers shared by the website and the hardware agent."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import unicodedata

from run_video_compiler import combine_run_to_target


PROJECT_DIRECTORY = Path(__file__).resolve().parent
LOCAL_RECORDINGS_DIRECTORY = (PROJECT_DIRECTORY / "recordings").resolve()
SERVER_RECORDINGS_DIRECTORY = Path(
    os.environ.get(
        "FURNACE_SERVER_RECORDINGS_DIRECTORY",
        r"Z:\Wu_Lab_Data\Liam\FurnaceRecordings",
    )
).expanduser().resolve()
SHORTENED_RUN_TARGET_MINUTES = 3.0


def create_run(run_name, run_type, details=None, recordings_root=None):
    """Create a named local run folder and return its video base path."""
    root = Path(recordings_root or LOCAL_RECORDINGS_DIRECTORY).resolve()
    display_name = str(run_name or "").strip() or "Untitled run"
    normalized = unicodedata.normalize("NFKD", display_name)
    safe_name = normalized.encode("ascii", "ignore").decode("ascii")
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", safe_name).strip("-").lower()
    safe_name = (safe_name or "untitled-run")[:60].rstrip("-")
    timestamp = datetime.now().strftime("%Y-%m-%d")
    run_directory = root / f"{timestamp}_{safe_name}"
    run_directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "run_name": display_name,
        "run_type": run_type,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "video_base": "video.mp4",
        "temperature_data": "video.timestamps.csv",
        **(details or {}),
    }
    (run_directory / "run.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return run_directory / "video.mp4", run_directory


def sync_run_to_server(run_directory, server_root=None):
    """Copy a completed local run directory to its archive directory."""
    source = Path(run_directory).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Run directory does not exist: {source}")

    destination_root = Path(
        server_root or SERVER_RECORDINGS_DIRECTORY
    ).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / source.name
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.copytree(source, destination)
    return destination


def create_shortened_run_upload(
    run_directory,
    target_minutes=SHORTENED_RUN_TARGET_MINUTES,
    recordings_root=None,
    server_root=None,
    compiler=None,
):
    """Create a shortened local clip and copy it beside the archived run."""
    source = Path(run_directory).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Run directory does not exist: {source}")

    root = Path(recordings_root or LOCAL_RECORDINGS_DIRECTORY).resolve()
    archive_root = Path(server_root or SERVER_RECORDINGS_DIRECTORY).resolve()
    local_output = source / f"{source.name}_shortened_{target_minutes:g}_min.mp4"
    (compiler or combine_run_to_target)(
        run_name=source.name,
        target_minutes=float(target_minutes),
        recordings_root=root,
        output_path=local_output,
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
    )

    server_run_directory = archive_root / source.name
    server_run_directory.mkdir(parents=True, exist_ok=True)
    server_output = server_run_directory / local_output.name
    if server_output.exists():
        if server_output.is_dir():
            shutil.rmtree(server_output)
        else:
            server_output.unlink()
    shutil.copy2(local_output, server_output)
    return local_output, server_output


def finalize_run_recording(
    run_directory,
    target_minutes=SHORTENED_RUN_TARGET_MINUTES,
    recordings_root=None,
    server_root=None,
    compiler=None,
):
    """Archive a run, then compile and archive its shortened summary video."""
    server_run_directory = sync_run_to_server(run_directory, server_root)
    local_summary, server_summary = create_shortened_run_upload(
        run_directory,
        target_minutes=target_minutes,
        recordings_root=recordings_root,
        server_root=server_root,
        compiler=compiler,
    )
    return server_run_directory, local_summary, server_summary