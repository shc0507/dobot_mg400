import json
import tempfile
import unittest
from pathlib import Path

from furnace.recordings import create_run, finalize_run_recording


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
                    "created_at": json.loads((run_directory / "run.json").read_text())[
                        "created_at"
                    ],
                    "video_base": "video.mp4",
                    "temperature_data": "video.timestamps.csv",
                },
            )

    def test_finalize_archives_run_and_compiled_summary(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "recordings"
            server_root = Path(temporary_directory) / "archive"
            _, run_directory = create_run("Anneal Batch 7", "agent_recording", recordings_root=root)
            (run_directory / "video_0001.mp4").write_bytes(b"segment")
            (run_directory / "video_0001.mp4").chmod(0o640)

            def compile_summary(**kwargs):
                self.assertEqual(kwargs["run_name"], run_directory.name)
                self.assertEqual(kwargs["target_minutes"], 3.0)
                kwargs["output_path"].write_bytes(b"shortened")
                kwargs["output_path"].chmod(0o640)

            archived_run, local_summary, server_summary = finalize_run_recording(
                run_directory,
                recordings_root=root,
                server_root=server_root,
                compiler=compile_summary,
            )

            self.assertEqual(archived_run, server_root.resolve() / run_directory.name)
            self.assertEqual((archived_run / "video_0001.mp4").read_bytes(), b"segment")
            self.assertEqual(local_summary.read_bytes(), b"shortened")
            self.assertEqual(server_summary.read_bytes(), b"shortened")


if __name__ == "__main__":
    unittest.main()


def private_file(path, content=b"segment"):
    path.write_bytes(content)
    path.chmod(0o640)
    return path


def summary_compiler(**kwargs):
    private_file(kwargs["output_path"], b"summary")


def test_local_only_finalization_never_creates_an_archive(tmp_path, monkeypatch):
    from furnace import recordings

    monkeypatch.delenv("MG400_FURNACE_ARCHIVE_DIR", raising=False)
    root = tmp_path / "local"
    _, run = create_run("Local", "manual_recording", recordings_root=root)
    segment = private_file(run / "video_0001.mp4")
    archived, summary, archived_summary = finalize_run_recording(
        run, recordings_root=root, compiler=summary_compiler
    )
    assert archived is None and archived_summary is None
    assert summary.read_bytes() == b"summary" and segment.read_bytes() == b"segment"
    assert sorted(tmp_path.iterdir()) == [root]
    # Empty archive selection does not even inspect an unavailable local path.
    assert recordings.sync_run_to_server("/not/a/real/run", server_root="") is None


def test_private_run_creation_refuses_collision_and_reserved_manifest_fields(tmp_path):
    import stat

    import pytest

    root = tmp_path / "local"
    _, run = create_run("Batch", "manual_recording", recordings_root=root)
    before = (run / "run.json").read_bytes()
    assert stat.S_IMODE(run.stat().st_mode) & 0o027 == 0
    assert stat.S_IMODE((run / "run.json").stat().st_mode) & 0o037 == 0
    with pytest.raises(FileExistsError):
        create_run("Batch", "manual_recording", recordings_root=root)
    with pytest.raises(ValueError, match="identity"):
        create_run("Other", "manual_recording", {"video_base": "escape"}, root)
    assert (run / "run.json").read_bytes() == before
    assert list(root.iterdir()) == [run]


def test_compiler_failure_preserves_local_run_and_does_not_create_archive(tmp_path):
    import pytest

    root, archive = tmp_path / "local", tmp_path / "archive"
    _, run = create_run("Failure", "manual_recording", recordings_root=root)
    segment = private_file(run / "video_0001.mp4")

    def fails(**kwargs):
        raise RuntimeError("fake compiler failed")

    with pytest.raises(RuntimeError, match="fake compiler"):
        finalize_run_recording(run, recordings_root=root, server_root=archive, compiler=fails)
    assert segment.read_bytes() == b"segment"
    assert not archive.exists()


def test_existing_archive_is_never_replaced_and_local_summary_survives(tmp_path):
    import pytest

    root, archive = tmp_path / "local", tmp_path / "archive"
    _, run = create_run("Collision", "manual_recording", recordings_root=root)
    private_file(run / "video_0001.mp4")
    archive.mkdir(mode=0o750)
    existing = archive / run.name
    existing.mkdir(mode=0o750)
    marker = private_file(existing / "operator-data", b"preserve")
    with pytest.raises(FileExistsError):
        finalize_run_recording(
            run, recordings_root=root, server_root=archive, compiler=summary_compiler
        )
    assert marker.read_bytes() == b"preserve"
    assert list(existing.iterdir()) == [marker]
    assert (run / f"{run.name}_shortened_3_min.mp4").read_bytes() == b"summary"
    assert (run / "video_0001.mp4").read_bytes() == b"segment"


def test_archive_verification_failure_keeps_local_files(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from furnace import recordings

    root, archive = tmp_path / "local", tmp_path / "archive"
    _, run = create_run("Verification", "manual_recording", recordings_root=root)
    private_file(run / "video_0001.mp4")
    monkeypatch.setattr(
        recordings.hashlib,
        "file_digest",
        lambda *args: SimpleNamespace(digest=lambda: b"incorrect hash"),
    )
    with pytest.raises(RuntimeError, match="verification"):
        finalize_run_recording(
            run, recordings_root=root, server_root=archive, compiler=summary_compiler
        )
    assert (run / "video_0001.mp4").read_bytes() == b"segment"
    assert (run / "run.json").is_file()


def test_private_helpers_refuse_traversal_and_ancestor_links_before_writes(tmp_path):
    import pytest

    from furnace import recordings

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        create_run("Unsafe", "manual_recording", recordings_root=link / "new")
    assert list(outside.iterdir()) == []
    with pytest.raises(ValueError, match="traversal"):
        create_run("Unsafe", "manual_recording", recordings_root=tmp_path / "a" / ".." / "b")
    assert not (tmp_path / "a").exists()
    root = tmp_path / "local"
    _, run = create_run("Safe", "manual_recording", recordings_root=root)
    with pytest.raises(ValueError, match="basename"):
        recordings.recording_output_path(run, "../outside/file", root)
    existing = private_file(run / "existing.mp4")
    with pytest.raises(FileExistsError):
        recordings.recording_output_path(run, existing.name, root)
    assert existing.read_bytes() == b"segment"


def test_archive_refuses_links_and_public_files_before_archive_creation(tmp_path):
    import os

    import pytest

    from furnace.recordings import sync_run_to_server

    for kind in ("symlink", "hardlink", "public"):
        root, archive = tmp_path / kind, tmp_path / f"archive-{kind}"
        _, run = create_run("Unsafe", "manual_recording", recordings_root=root)
        source = private_file(run / "video_0001.mp4")
        if kind == "symlink":
            (run / "linked.mp4").symlink_to(source)
        elif kind == "hardlink":
            os.link(source, run / "linked.mp4")
        else:
            source.chmod(0o644)
        with pytest.raises((OSError, ValueError)):
            sync_run_to_server(run, archive, recordings_root=root)
        assert source.read_bytes() == b"segment"
        assert not archive.exists()


def test_persistent_recording_identity_is_exclusive_and_manifest_reserved(tmp_path):
    import pytest

    root = tmp_path / "local"
    identity = "0123456789abcdef" * 2
    _, run = create_run(
        "First label", "manual_recording", recordings_root=root, recording_id=identity
    )
    assert run.name == identity
    manifest = json.loads((run / "run.json").read_text())
    assert manifest["recording_id"] == identity
    assert manifest["video_base"] == "video.mp4"
    assert manifest["temperature_data"] == "video.timestamps.csv"
    with pytest.raises(FileExistsError):
        create_run(
            "Different label", "manual_recording", recordings_root=root, recording_id=identity
        )
    with pytest.raises(ValueError, match="identity"):
        create_run("Override", "manual_recording", {"recording_id": "bad"}, root)
    assert json.loads((run / "run.json").read_text())["run_name"] == "First label"
    for invalid in ("", "../escape", "A" * 32, "x" * 32, "a" * 31, True):
        with pytest.raises(ValueError, match="recording_id"):
            create_run("Bad", "manual_recording", recordings_root=root, recording_id=invalid)
    assert list(root.iterdir()) == [run]
