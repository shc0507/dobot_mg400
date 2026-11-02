"""Combine one run's recording segments into a single accelerated video."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from .recordings import (
    LOCAL_RECORDINGS_DIRECTORY,
    _path,
    directory_fd,
    private_open,
    recording_output_path,
    validated_run_directory,
)

DEFAULT_RECORDINGS_ROOT = LOCAL_RECORDINGS_DIRECTORY


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def find_run_directory(recordings_root: str | Path, run_name: str) -> Path:
    """Return the directory for a run, matching either the manifest name or folder name."""
    root = _path(recordings_root)
    with directory_fd(root, root=root):
        pass

    query = _normalize(run_name)
    if not query:
        raise ValueError("A run name or run folder name is required.")

    candidates: list[Path] = []
    # A supplied folder identity takes precedence over fuzzy display-name matching.
    direct = root / run_name
    if direct.parent == root and direct.exists():
        return validated_run_directory(direct, root)
    for path in sorted(root.iterdir()):
        if path.is_symlink():
            raise ValueError("Recording run directories must not be symbolic links")
        if not path.is_dir():
            continue
        validated_run_directory(path, root)
        names = [path.name]
        manifest_path = path / "run.json"
        if manifest_path.exists():
            try:
                with private_open(manifest_path, "r", root=root, encoding="utf-8") as stream:
                    manifest = json.load(stream)
            except json.JSONDecodeError:
                manifest = {}
            run_label = manifest.get("run_name")
            if run_label:
                names.append(str(run_label))

        exact = [name for name in names if _normalize(name) == query]
        if exact:
            candidates.append(path)
            continue

        contains = [name for name in names if query in _normalize(name)]
        if contains:
            candidates.append(path)

    if not candidates:
        raise FileNotFoundError(f"No run matching '{run_name}' was found inside {root}.")

    return candidates[0]


def list_segment_files(run_dir: str | Path) -> list[Path]:
    """Return video files in the run folder in chronological order."""
    directory = _path(run_dir)
    with directory_fd(directory, root=directory):
        pass
    files = sorted(directory.glob("video*.mp4"), key=_segment_sort_key)
    if files:
        for path in files:
            with private_open(path, "rb", root=directory):
                pass
        return files

    single = directory / "video.mp4"
    if single.exists():
        return [single]

    raise FileNotFoundError(f"No video files were found in run directory: {directory}.")


def _segment_sort_key(path: Path):
    match = re.search(r"video(?:_(\d+))?\.mp4$", path.name, flags=re.IGNORECASE)
    if match is None:
        return (0, path.name)
    index_text = match.group(1)
    return (1, int(index_text) if index_text else 0, path.name)


def speed_factor_for_target(actual_minutes: float, target_minutes: float) -> float:
    """Return the playback speed multiplier for a target duration in minutes."""
    if isinstance(target_minutes, bool) or not math.isfinite(target_minutes) or target_minutes <= 0:
        raise ValueError("Target length must be finite and greater than zero minutes.")
    if isinstance(actual_minutes, bool) or not math.isfinite(actual_minutes):
        raise ValueError("Actual length must be finite.")
    if actual_minutes <= 0:
        return 1.0
    # Keep playback at 1x when the target is equal or longer than the original recording.
    return max(1.0, actual_minutes / target_minutes)


def _ffprobe_duration(ffprobe_path: str, video_path: Path) -> float:
    cmd = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=30, umask=0o077
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"ffprobe could not read duration for {video_path}: {message}")

    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"ffprobe returned no duration for {video_path}")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise RuntimeError("ffprobe returned an invalid duration")
    return seconds


def _concat_segments(segments: Iterable[Path], concat_path: Path) -> None:
    # FFmpeg concat quoting must also preserve apostrophes in a configured root.
    names = [segment.as_posix().replace("'", "'\\''") for segment in segments]
    content = "\n".join(f"file '{name}'" for name in names) + "\n"
    with private_open(concat_path, "x", root=concat_path.parent, encoding="utf-8") as output:
        output.write(content)


def _run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=300, umask=0o077
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "Unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {stderr}")


def combine_run_to_target(
    run_name: str,
    target_minutes: float,
    recordings_root: str | Path | None = None,
    output_path: str | Path | None = None,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> Path:
    """Create a shortened single video for a run using ffmpeg."""
    speed_factor_for_target(1, target_minutes)
    root = _path(recordings_root) if recordings_root is not None else DEFAULT_RECORDINGS_ROOT
    run_dir = find_run_directory(root, run_name)
    segments = list_segment_files(run_dir)
    if output_path is None:
        output_path = run_dir / f"{run_dir.name}_shortened_{target_minutes:g}_min.mp4"
    output_path = _path(output_path)
    if output_path.parent != run_dir or output_path.suffix != ".mp4":
        raise ValueError("The summary must be an MP4 basename inside the selected run")
    output_path = recording_output_path(run_dir, output_path.name, root)
    if shutil.which(ffprobe_path) is None or shutil.which(ffmpeg_path) is None:
        raise RuntimeError("FFmpeg and ffprobe are required to compile a recording summary")
    total_seconds = sum(_ffprobe_duration(ffprobe_path, segment) for segment in segments)
    actual_minutes = total_seconds / 60.0
    speed = speed_factor_for_target(actual_minutes, target_minutes)
    # Only this invocation's private temporary directory is removed on failure.
    with tempfile.TemporaryDirectory(prefix=".compile-", dir=run_dir) as temporary:
        temporary = Path(temporary)
        concat_path = temporary / "segments.txt"
        _concat_segments(segments, concat_path)
        temp_output = temporary / "summary.mp4"
        ffmpeg_cmd = [
            ffmpeg_path,
            "-n",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-filter:v",
            f"setpts=PTS/{speed}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(temp_output),
        ]
        _run_ffmpeg(ffmpeg_cmd)
        with private_open(temp_output, "rb", root=temporary) as summary:
            if os.fstat(summary.fileno()).st_size == 0:
                raise RuntimeError("FFmpeg produced an empty summary")
        # A hard link publishes atomically and refuses any existing destination.
        with directory_fd(temporary, root=root) as source_fd:
            with directory_fd(run_dir, root=root) as target_fd:
                os.link(
                    temp_output.name,
                    output_path.name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=target_fd,
                    follow_symlinks=False,
                )
                os.unlink(temp_output.name, dir_fd=source_fd)

    print(
        f"Created {output_path} from {len(segments)} segment(s). "
        f"Original duration: {actual_minutes:.2f} min, target: {target_minutes:.2f} min, "
        f"speedup: {speed:.2f}x"
    )
    return output_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Combine the video segments from one run into a single mp4 and speed it up "
            "so the total playback time matches the requested length in minutes."
        )
    )
    parser.add_argument("run_name", help="Run folder name or manifest run name to process")
    parser.add_argument("target_minutes", type=float, help="Desired output duration in minutes")
    parser.add_argument(
        "--recordings-root",
        type=Path,
        default=DEFAULT_RECORDINGS_ROOT,
        help="Directory containing private run folders. Defaults to /var/lib/mg400/recordings",
    )
    parser.add_argument("--output", type=Path, help="Optional output path for the shortened video")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="Path to the ffmpeg executable")
    parser.add_argument("--ffprobe", default="ffprobe", help="Path to the ffprobe executable")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        combine_run_to_target(
            run_name=args.run_name,
            target_minutes=args.target_minutes,
            recordings_root=args.recordings_root,
            output_path=args.output,
            ffmpeg_path=args.ffmpeg,
            ffprobe_path=args.ffprobe,
        )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
