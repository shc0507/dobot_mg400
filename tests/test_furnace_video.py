import tempfile
import unittest
from pathlib import Path

from furnace.video import find_run_directory, list_segment_files, speed_factor_for_target


class RunVideoCompilerTests(unittest.TestCase):
    def test_find_run_directory_matches_run_manifest_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "recordings"
            run_dir = root / "2026-09-27_10-00-00-000001_example-run"
            root.mkdir(mode=0o750)
            run_dir.mkdir(mode=0o750)
            (run_dir / "run.json").write_text(
                '{"run_name": "Example Run"}',
                encoding="utf-8",
            )

            (run_dir / "run.json").chmod(0o640)
            self.assertEqual(find_run_directory(root, "example run"), run_dir)

    def test_list_segment_files_returns_numbered_videos_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "video_0001.mp4").write_bytes(b"a")
            (run_dir / "video_0002.mp4").write_bytes(b"b")
            (run_dir / "video_0003.mp4").write_bytes(b"c")
            (run_dir / "notes.txt").write_text("ignored", encoding="utf-8")

            for path in run_dir.glob("video*.mp4"):
                path.chmod(0o640)
            segments = list_segment_files(run_dir)

            self.assertEqual(
                segments,
                [
                    run_dir / "video_0001.mp4",
                    run_dir / "video_0002.mp4",
                    run_dir / "video_0003.mp4",
                ],
            )

    def test_speed_factor_for_target_clamps_to_one_when_target_is_longer(self):
        self.assertAlmostEqual(speed_factor_for_target(30.0, 45.0), 1.0)
        self.assertAlmostEqual(speed_factor_for_target(30.0, 5.0), 6.0)


if __name__ == "__main__":
    unittest.main()


def private_video(path, data=b"segment"):
    path.write_bytes(data)
    path.chmod(0o640)
    return path


def test_video_compilation_uses_ordered_segments_and_private_no_replace_output(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from furnace import video
    from furnace.recordings import create_run

    root = tmp_path / "camera's recordings"
    _, run = create_run("Example", "manual_recording", recordings_root=root)
    for number in (10, 2, 1):
        private_video(run / f"video_{number:04}.mp4")
    calls = []

    def fake_process(command, **kwargs):
        calls.append((command, kwargs))
        assert kwargs["umask"] == 0o077 and kwargs["timeout"] > 0
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="120", stderr="")
        assert "-n" in command and "-y" not in command
        manifest = Path(command[command.index("-i") + 1]).read_text()
        assert "camera'\\''s recordings" in manifest
        assert manifest.index("video_0001.mp4") < manifest.index("video_0002.mp4")
        assert manifest.index("video_0002.mp4") < manifest.index("video_0010.mp4")
        assert command[command.index("-filter:v") + 1] == "setpts=PTS/2.0"
        private_video(Path(command[-1]), b"compiled")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(video.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(video.subprocess, "run", fake_process)
    result = video.combine_run_to_target(run.name, 3, recordings_root=root)
    assert result.read_bytes() == b"compiled" and result.stat().st_nlink == 1
    assert len(calls) == 4
    assert not list(run.glob(".compile-*"))
    assert len(list(run.glob("video_*.mp4"))) == 3


def test_video_output_escape_or_collision_never_invokes_compiler(tmp_path, monkeypatch):
    import pytest

    from furnace import video
    from furnace.recordings import create_run

    root = tmp_path / "local"
    _, run = create_run("Example", "manual_recording", recordings_root=root)
    segment = private_video(run / "video.mp4")
    monkeypatch.setattr(video.subprocess, "run", lambda *args, **kwargs: pytest.fail("spawned"))
    for output in (tmp_path / "escape.mp4", run / ".." / "escape.mp4"):
        with pytest.raises(ValueError):
            video.combine_run_to_target(run.name, 3, root, output)
    with pytest.raises(FileExistsError):
        video.combine_run_to_target(run.name, 3, root, segment)
    assert segment.read_bytes() == b"segment"


def test_failed_compilation_keeps_segments_and_unrelated_files(tmp_path, monkeypatch):
    import pytest

    from furnace import video
    from furnace.recordings import create_run

    root = tmp_path / "local"
    _, run = create_run("Failure", "manual_recording", recordings_root=root)
    segment = private_video(run / "video.mp4")
    marker = private_video(run / "unrelated.tmp.mp4", b"operator-data")
    monkeypatch.setattr(video.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(video, "_ffprobe_duration", lambda *args: 300)

    def fail(command):
        private_video(Path(command[-1]), b"partial")
        raise RuntimeError("failed compilation")

    monkeypatch.setattr(video, "_run_ffmpeg", fail)
    with pytest.raises(RuntimeError, match="failed compilation"):
        video.combine_run_to_target(run.name, 3, root)
    assert segment.read_bytes() == b"segment"
    assert marker.read_bytes() == b"operator-data"
    assert not list(run.glob(".compile-*"))


def test_explicit_folder_identity_wins_over_fuzzy_manifest_match(tmp_path):
    from furnace.recordings import create_run

    root = tmp_path / "local"
    _, expected = create_run("Expected", "manual_recording", recordings_root=root)
    other = root / "0000-other"
    other.mkdir(mode=0o750)
    private_video(other / "run.json", ('{"run_name":"prefix ' + expected.name + '"}').encode())
    assert find_run_directory(root, expected.name) == expected


def test_nonfinite_video_durations_are_rejected():
    import pytest

    for value in (float("nan"), float("inf"), -1, 0, True):
        with pytest.raises(ValueError):
            speed_factor_for_target(30, value)
