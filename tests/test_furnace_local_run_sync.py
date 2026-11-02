import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from furnace import recordings as app


class LocalRunSyncTests(unittest.TestCase):
    def test_shortened_version_is_copied_into_server_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = Path(tmp) / "local-recordings"
            server_root = Path(tmp) / "server-recordings"
            run_dir = local_root / "2026-09-27_example-run"
            local_root.mkdir(mode=0o750)
            run_dir.mkdir(mode=0o750)

            def write_summary(**kwargs):
                kwargs["output_path"].write_bytes(b"summary")
                kwargs["output_path"].chmod(0o640)

            local_summary, server_summary = app.create_shortened_run_upload(
                run_dir,
                recordings_root=local_root,
                server_root=server_root,
                compiler=write_summary,
            )
            expected_server_path = server_root / run_dir.name / local_summary.name
            self.assertEqual(server_summary, expected_server_path)
            self.assertEqual(server_summary.read_bytes(), b"summary")

    def test_normalize_recording_settings_defaults_to_time_lapse(self):
        settings = app.normalize_recording_settings({})

        self.assertEqual(settings["mode"], "time_lapse")
        self.assertEqual(settings["capture_interval_seconds"], 30.0)
        self.assertEqual(settings["output_fps"], 1.0)
        self.assertIsNone(settings["minimum_free_gb"])

    def test_normalize_recording_settings_preserves_time_lapse_mode(self):
        settings = app.normalize_recording_settings(
            {
                "recording_mode": "time_lapse",
                "capture_interval_seconds": "30",
                "output_fps": "2",
            }
        )

        self.assertEqual(settings["mode"], "time_lapse")
        self.assertEqual(settings["capture_interval_seconds"], 30.0)
        self.assertEqual(settings["output_fps"], 2.0)

    def test_normalize_recording_settings_accepts_hyphenated_time_lapse_mode(self):
        settings = app.normalize_recording_settings(
            {
                "mode": "time-lapse",
                "capture_interval_seconds": "45",
                "output_fps": "1.5",
            }
        )

        self.assertEqual(settings["mode"], "time_lapse")
        self.assertEqual(settings["capture_interval_seconds"], 45.0)
        self.assertEqual(settings["output_fps"], 1.5)

    def test_finalize_run_recording_syncs_and_uploads_shortened_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            local_root = Path(tmp) / "local-recordings"
            server_root = Path(tmp) / "server-recordings"
            run_dir = local_root / "2026-09-27_example-run"
            local_summary = run_dir / "example-run_shortened_3_min.mp4"
            server_summary = server_root / run_dir.name / local_summary.name
            with (
                patch.object(
                    app, "sync_run_to_server", return_value=server_root / run_dir.name
                ) as sync_mock,
                patch.object(
                    app, "create_shortened_run_upload", return_value=(local_summary, None)
                ) as upload_mock,
            ):
                result = app.finalize_run_recording(
                    run_dir, recordings_root=local_root, server_root=server_root
                )
                self.assertEqual(
                    result, (server_root / run_dir.name, local_summary, server_summary)
                )
                sync_mock.assert_called_once_with(run_dir, server_root, recordings_root=local_root)
                upload_mock.assert_called_once_with(
                    run_dir,
                    target_minutes=3.0,
                    recordings_root=local_root,
                    server_root="",
                    compiler=None,
                )


if __name__ == "__main__":
    unittest.main()


def test_invalid_timing_and_disk_values_are_rejected():
    import pytest

    for key in ("capture_interval_seconds", "output_fps", "minimum_free_gb"):
        for value in (float("nan"), float("inf"), -1, True):
            with pytest.raises(ValueError):
                app.normalize_recording_settings({key: value})
    assert app.normalize_recording_settings({"mode": "live"}) == {
        "mode": "live",
        "capture_interval_seconds": None,
        "output_fps": 30.0,
        "minimum_free_gb": 2.0,
    }
