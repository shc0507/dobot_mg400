"""Compile and run actual firmware against a simulated three-axis stage (no hardware)."""

import argparse
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", default="g++", help="C++ compiler executable")
    parser.add_argument(
        "--compiler-arg",
        action="append",
        default=[],
        help="Extra compiler argument, e.g. --compiler-arg=c++ for zig",
    )
    parser.add_argument(
        "--trace-output",
        type=Path,
        help="Save actual firmware status/ACK/TEL output from a simulated 0.1 turn",
    )
    parser.add_argument(
        "--calibration-trace-output",
        type=Path,
        help="Save status/ACK/TEL/LIM from an autonomous calibration",
    )
    parser.add_argument(
        "--positioning-trace-output",
        type=Path,
        help="Save UART output from calibration followed by two absolute moves",
    )
    parser.add_argument(
        "--warning-trace-output",
        type=Path,
        help="Save UART output from six spaced held-direction warning incidents",
    )
    args = parser.parse_args()
    sketch = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="stage-feedback-") as directory:
        binary = Path(directory) / "native.exe"
        command = [
            args.compiler,
            *args.compiler_arg,
            "-std=c++11",
            "-DARDUINO=10819",
            "-UNDEBUG",
            "-Wall",
            "-Wextra",
            "-Wno-missing-field-initializers",
            "-O2",
            "-I",
            str(sketch / "tests"),
            str(sketch / "tests/native.cpp"),
            str(sketch / "Encoders.cpp"),
            "-o",
            str(binary),
        ]
        subprocess.run(command, check=True)
        subprocess.run([str(binary)], check=True, timeout=60)
        if args.trace_output:
            trace = subprocess.run(
                [str(binary), "--trace"], check=True, capture_output=True, timeout=60
            )
            args.trace_output.parent.mkdir(parents=True, exist_ok=True)
            args.trace_output.write_bytes(trace.stdout)
            print(f"Firmware telemetry trace saved to {args.trace_output.resolve()}")
        if args.calibration_trace_output:
            trace = subprocess.run(
                [str(binary), "--calibration-trace"], check=True, capture_output=True, timeout=60
            )
            args.calibration_trace_output.parent.mkdir(parents=True, exist_ok=True)
            args.calibration_trace_output.write_bytes(trace.stdout)
            print(f"Firmware calibration trace saved to {args.calibration_trace_output.resolve()}")
        if args.positioning_trace_output:
            trace = subprocess.run(
                [str(binary), "--positioning-trace"], check=True, capture_output=True, timeout=60
            )
            args.positioning_trace_output.parent.mkdir(parents=True, exist_ok=True)
            args.positioning_trace_output.write_bytes(trace.stdout)
            print(f"Firmware positioning trace saved to {args.positioning_trace_output.resolve()}")
        if args.warning_trace_output:
            trace = subprocess.run(
                [str(binary), "--warning-trace"], check=True, capture_output=True, timeout=60
            )
            args.warning_trace_output.parent.mkdir(parents=True, exist_ok=True)
            args.warning_trace_output.write_bytes(trace.stdout)
            print(f"Firmware warning trace saved to {args.warning_trace_output.resolve()}")


if __name__ == "__main__":
    main()
