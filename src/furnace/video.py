"""Combine one run's recording segments into a single accelerated video."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


DEFAULT_RECORDINGS_ROOT = Path(__file__).resolve().parent / "recordings"


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def find_run_directory(recordings_root: str | Path, run_name: str) -> Path:
    """Return the directory for a run, matching either the manifest name or folder name."""
    root = Path(recordings_root)
    if not root.exists():
        raise FileNotFoundError(f"Recordings directory does not exist: {root}")

    query = _normalize(run_name)
    if not query:
        raise ValueError("A run name or run folder name is required.")

    candidates: list[Path] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        names = [path.name]
        manifest_path = path / "run.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
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
        raise FileNotFoundError(
            f"No run matching '{run_name}' was found inside {root}."
        )

    return candidates[0]


def list_segment_files(run_dir: str | Path) -> list[Path]:
    """Return video files in the run folder in chronological order."""
    directory = Path(run_dir)
    if not directory.exists():
        raise FileNotFoundError(f"Run directory does not exist: {directory}")

    files = sorted(directory.glob("video*.mp4"), key=_segment_sort_key)
    if files:
        return files

    single = directory / "video.mp4"
    if single.exists():
        return [single]

    raise FileNotFoundError(
        f"No video files were found in run directory: {directory}."
    )


def _segment_sort_key(path: Path):
    match = re.search(r"video(?:_(\d+))?\.mp4$", path.name, flags=re.IGNORECASE)
    if match is None:
        return (0, path.name)
    index_text = match.group(1)
    return (1, int(index_text) if index_text else 0, path.name)


def speed_factor_for_target(actual_minutes: float, target_minutes: float) -> float:
    """Return the playback speed multiplier for a target duration in minutes."""
    if actual_minutes <= 0:
        return 1.0
    if target_minutes <= 0:
        raise ValueError("Target length must be greater than zero minutes.")
    # Keep playback at 1x when the target is equal or longer than the original recording.
    return max(1.0, actual_minutes / target_minutes)


def _ffprobe_duration(ffprobe_path: str, video_path: Path) -> float:
    cmd = [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe could not read duration for {video_path}: {result.stderr.strip() or result.stdout.strip()}")

    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"ffprobe returned no duration for {video_path}")
    return float(value)


def _concat_segments(segments: Iterable[Path], concat_path: Path) -> None:
    concat_content = "\n".join(f"file '{segment.as_posix()}'" for segment in segments)
    concat_path.write_text(concat_content + "\n", encoding="utf-8")


def _run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
    if target_minutes <= 0:
        raise ValueError("Target length must be greater than zero minutes.")

    root = Path(recordings_root) if recordings_root is not None else DEFAULT_RECORDINGS_ROOT
    run_dir = find_run_directory(root, run_name)
    segments = list_segment_files(run_dir)

    if shutil.which(ffprobe_path) is None:
        raise RuntimeError(
            "ffprobe is required to calculate the current run duration. Install FFmpeg and ensure ffprobe is on PATH."
        )

    total_seconds = sum(_ffprobe_duration(ffprobe_path, segment) for segment in segments)
    actual_minutes = total_seconds / 60.0
    speed = speed_factor_for_target(actual_minutes, target_minutes)

    if output_path is None:
        output_path = run_dir / f"{run_dir.name}_shortened_{target_minutes:g}_min.mp4"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    concat_path = run_dir / f"{run_dir.name}.concat.txt"
    _concat_segments(segments, concat_path)

    temp_output = output_path.with_suffix(".tmp.mp4")
    if temp_output.exists():
        temp_output.unlink()

    ffmpeg_cmd = [
        ffmpeg_path,
        "-y",
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

    if output_path.exists():
        output_path.unlink()
    temp_output.replace(output_path)

    if concat_path.exists():
        concat_path.unlink()

    print(
        f"Created {output_path} from {len(segments)} segment(s). "
        f"Original duration: {actual_minutes:.2f} min, target: {target_minutes:.2f} min, speedup: {speed:.2f}x"
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
        help="Directory containing run folders. Defaults to ./recordings",
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
