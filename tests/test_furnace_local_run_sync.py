import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class LocalRunSyncTests(unittest.TestCase):
    def test_shortened_version_is_copied_into_server_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = Path(tmp) / "local-recordings"
            server_root = Path(tmp) / "server-recordings"
            run_dir = local_root / "2026-09-27_example-run"
            run_dir.mkdir(parents=True)

            original_local = app.LOCAL_RECORDINGS_DIRECTORY
            original_server = app.SERVER_RECORDINGS_DIRECTORY
            app.LOCAL_RECORDINGS_DIRECTORY = local_root
            app.SERVER_RECORDINGS_DIRECTORY = server_root

            def write_summary(**kwargs):
                kwargs["output_path"].write_bytes(b"summary")

            try:
                with patch.object(app, "combine_run_to_target", side_effect=write_summary):
                    local_summary, server_summary = app.create_shortened_run_upload(run_dir)

                expected_server_path = server_root / run_dir.name / local_summary.name
                self.assertEqual(server_summary, expected_server_path)
                self.assertEqual(server_summary.read_bytes(), b"summary")
            finally:
                app.LOCAL_RECORDINGS_DIRECTORY = original_local
                app.SERVER_RECORDINGS_DIRECTORY = original_server

    def test_normalize_recording_settings_defaults_to_time_lapse(self):
        settings = app.normalize_recording_settings({})

        self.assertEqual(settings["mode"], "time_lapse")
        self.assertEqual(settings["capture_interval_seconds"], 30.0)
        self.assertEqual(settings["output_fps"], 1.0)
        self.assertIsNone(settings["minimum_free_gb"])

    def test_normalize_recording_settings_preserves_time_lapse_mode(self):
        settings = app.normalize_recording_settings({
            "recording_mode": "time_lapse",
            "capture_interval_seconds": "30",
            "output_fps": "2",
        })

        self.assertEqual(settings["mode"], "time_lapse")
        self.assertEqual(settings["capture_interval_seconds"], 30.0)
        self.assertEqual(settings["output_fps"], 2.0)

    def test_normalize_recording_settings_accepts_hyphenated_time_lapse_mode(self):
        settings = app.normalize_recording_settings({
            "mode": "time-lapse",
            "capture_interval_seconds": "45",
            "output_fps": "1.5",
        })

        self.assertEqual(settings["mode"], "time_lapse")
        self.assertEqual(settings["capture_interval_seconds"], 45.0)
        self.assertEqual(settings["output_fps"], 1.5)

    def test_finalize_run_recording_syncs_and_uploads_shortened_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = Path(tmp) / "local-recordings"
            server_root = Path(tmp) / "server-recordings"
            run_dir = local_root / "2026-09-27_example-run"
            run_dir.mkdir(parents=True)
            (run_dir / "video.mp4").write_bytes(b"video")
            (run_dir / "run.json").write_text('{"run_name": "Example Run"}', encoding="utf-8")

            original_local = app.LOCAL_RECORDINGS_DIRECTORY
            original_server = app.SERVER_RECORDINGS_DIRECTORY
            app.LOCAL_RECORDINGS_DIRECTORY = local_root
            app.SERVER_RECORDINGS_DIRECTORY = server_root
            try:
                server_summary = server_root / run_dir.name / "example-run_shortened_3_min.mp4"
                with patch.object(app, "sync_run_to_server", return_value=server_root / run_dir.name) as sync_mock, \
                     patch.object(app, "create_shortened_run_upload", return_value=(run_dir / "example-run_shortened_3_min.mp4", server_summary)) as upload_mock:
                    result = app.finalize_run_recording(run_dir)

                    self.assertEqual(result[0], run_dir / "example-run_shortened_3_min.mp4")
                    self.assertEqual(result[1], server_summary)
                    sync_mock.assert_called_once_with(run_dir)
                    upload_mock.assert_called_once_with(run_dir, target_minutes=3.0)
            finally:
                app.LOCAL_RECORDINGS_DIRECTORY = original_local
                app.SERVER_RECORDINGS_DIRECTORY = original_server


if __name__ == "__main__":
    unittest.main()
