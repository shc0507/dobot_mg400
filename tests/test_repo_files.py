"""Keep large assets and binary artifacts out of the repository."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BINARY_EXTENSIONS = {
    ".dll",
    ".so",
    ".dylib",
    ".exe",
    ".bin",
    ".pt",
    ".pth",
    ".onnx",
    ".engine",
    ".npz",
    ".h5",
    ".zip",
    ".stp",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


def test_tracked_files_meet_asset_policy():
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable, so tracked files cannot be checked.")
    if not (ROOT / ".git").exists():
        pytest.skip("The .git entry is absent, so tracked files cannot be checked.")

    tracked = subprocess.run(
        [git, "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    violations = []
    for name in tracked.split(b"\0"):
        if not name:
            continue
        relative = os.fsdecode(name)
        path = ROOT / relative
        size = path.stat().st_size
        extension = path.suffix.lower()
        if extension in BINARY_EXTENSIONS:
            violations.append(f"{relative!r}: prohibited binary extension {extension}")
        if relative != "uv.lock" and size > 1_000_000:
            violations.append(f"{relative!r}: {size:,} bytes exceeds the 1,000,000-byte limit")
        if extension in IMAGE_EXTENSIONS and size > 300_000:
            violations.append(f"{relative!r}: {size:,} bytes exceeds the 300,000-byte image limit")

    assert not violations, (
        "Tracked files violate the asset policy:\n"
        + "\n".join(violations)
        + "\nStore these assets through the asset manifest (FND-08), "
        "in host data under /var/lib/mg400, or in a release."
    )
