import tempfile
import unittest
from pathlib import Path

from run_video_compiler import find_run_directory, list_segment_files, speed_factor_for_target


class RunVideoCompilerTests(unittest.TestCase):
    def test_find_run_directory_matches_run_manifest_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "recordings"
            run_dir = root / "2026-09-27_10-00-00-000001_example-run"
            run_dir.mkdir(parents=True)
            (run_dir / "run.json").write_text(
                '{"run_name": "Example Run"}',
                encoding="utf-8",
            )

            self.assertEqual(find_run_directory(root, "example run"), run_dir)

    def test_list_segment_files_returns_numbered_videos_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "video_0001.mp4").write_bytes(b"a")
            (run_dir / "video_0002.mp4").write_bytes(b"b")
            (run_dir / "video_0003.mp4").write_bytes(b"c")
            (run_dir / "notes.txt").write_text("ignored", encoding="utf-8")

            segments = list_segment_files(run_dir)

            self.assertEqual(segments, [
                run_dir / "video_0001.mp4",
                run_dir / "video_0002.mp4",
                run_dir / "video_0003.mp4",
            ])

    def test_speed_factor_for_target_clamps_to_one_when_target_is_longer(self):
        self.assertAlmostEqual(speed_factor_for_target(30.0, 45.0), 1.0)
        self.assertAlmostEqual(speed_factor_for_target(30.0, 5.0), 6.0)


if __name__ == "__main__":
    unittest.main()
