"""Build and run GPIO-only native checks using an installed AccelStepper source tree."""

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--library", required=True, type=Path, help="AccelStepper library directory"
    )
    args = parser.parse_args()
    library = args.library.resolve() / "src"
    if not (library / "AccelStepper.cpp").is_file():
        parser.error("--library must contain src/AccelStepper.cpp")
    sketch = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="stage-native-") as directory:
        root = Path(directory)
        copied = root / "sketch"
        shutil.copytree(sketch, copied)
        flags = ["g++", "-std=c++11", "-DARDUINO=10819", "-Wall", "-Wextra"]
        include = ["-I", str(copied / "tests"), "-I", str(library)]
        build = [
            *flags,
            "-Wno-missing-field-initializers",
            *include,
            str(copied / "tests/native.cpp"),
            str(copied / "MotorControl.cpp"),
            str(library / "AccelStepper.cpp"),
            "-o",
            str(root / "native"),
        ]
        subprocess.run(build, check=True)
        subprocess.run([str(root / "native")], check=True, timeout=30)
        pins = copied / "MotorPins.h"
        original = pins.read_text()
        # Exercise the retained switch safeguards only in this temporary copy.
        # The production header always represents the stage without switches.
        switched = original
        for name, pin in (
            ("InsertLimitMin", "A0"),
            ("InsertLimitMax", "A1"),
            ("BottomLimitMin", "A2"),
            ("BottomLimitMax", "A3"),
            ("TopLimitMin", "A4"),
            ("TopLimitMax", "A5"),
        ):
            switched = switched.replace(f"{name} = 255", f"{name} = {pin}")
        pins.write_text(switched)
        subprocess.run(build, check=True)
        subprocess.run([str(root / "native")], check=True, timeout=30)
        for invalid in ("A0", "5", "0", "20"):
            pins.write_text(switched.replace("BottomLimitMin = A2", f"BottomLimitMin = {invalid}"))
            probe = root / "pins.cpp"
            probe.write_text('#include "sketch/MotorPins.h"\n')
            result = subprocess.run(
                [*flags, *include, "-fsyntax-only", str(probe)], capture_output=True, text=True
            )
            if result.returncode == 0 or "static assertion failed" not in result.stderr:
                raise AssertionError(f"Pin conflict/range {invalid} was not rejected")
        print("static pin conflict/range checks passed")


if __name__ == "__main__":
    main()
