import json
import tempfile
import unittest
from pathlib import Path

from run_recordings import create_run, finalize_run_recording


class RunRecordingsTests(unittest.TestCase):
    def test_create_run_writes_manifest_in_named_local_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "recordings"
            video_path, run_directory = create_run(
                "Anneal Batch 7", "agent_recording", recordings_root=root
            )

            self.assertEqual(video_path, run_directory / "video.mp4")
            self.assertEqual(run_directory.parent, root.resolve())
            self.assertEqual(
                json.loads((run_directory / "run.json").read_text()),
                {
                    "run_name": "Anneal Batch 7",
                    "run_type": "agent_recording",
                    "created_at": json.loads(
                        (run_directory / "run.json").read_text()
                    )["created_at"],
                    "video_base": "video.mp4",
                    "temperature_data": "video.timestamps.csv",
                },
            )

    def test_finalize_archives_run_and_compiled_summary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "recordings"
            server_root = Path(temporary_directory) / "archive"
            _, run_directory = create_run(
                "Anneal Batch 7", "agent_recording", recordings_root=root
            )
            (run_directory / "video_0001.mp4").write_bytes(b"segment")

            def compile_summary(**kwargs):
                self.assertEqual(kwargs["run_name"], run_directory.name)
                self.assertEqual(kwargs["target_minutes"], 3.0)
                kwargs["output_path"].write_bytes(b"shortened")

            archived_run, local_summary, server_summary = finalize_run_recording(
                run_directory,
                recordings_root=root,
                server_root=server_root,
                compiler=compile_summary,
            )

            self.assertEqual(
                archived_run, server_root.resolve() / run_directory.name
            )
            self.assertEqual(
                (archived_run / "video_0001.mp4").read_bytes(), b"segment"
            )
            self.assertEqual(local_summary.read_bytes(), b"shortened")
            self.assertEqual(server_summary.read_bytes(), b"shortened")


if __name__ == "__main__":
    unittest.main()