"""One-way objective rotation with one full-resolution USB photo per move."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import threading
import time

import cv2


class SideCaptureCancelled(RuntimeError):
    """The operator cancelled a side-camera acquisition."""


@dataclass(frozen=True)
class SideCaptureProgress:
    saved: int
    total: int
    commanded_steps: int
    directory: Path


def capture_objective_series(
    camera, motor, directory, *, step=2, total_steps=720,
    speed=200, settle_seconds=0.35, cancel_event=None, progress=None,
):
    """Move +2 full steps, await DONE, then save a fresh photo; repeat to +720.

    Firmware position and commanded travel are recorded separately because
    neither is a measurement of actual mechanical motion. No return move is
    performed, and the objective is disabled in every exit path.
    """
    if step <= 0 or total_steps <= 0 or total_steps % step:
        raise ValueError("Total steps must be a positive multiple of step")
    if speed <= 0 or settle_seconds < 0:
        raise ValueError("Speed must be positive and settling nonnegative")
    cancel_event = cancel_event or threading.Event()
    directory = Path(directory)
    total = total_steps // step

    def check_cancelled():
        if cancel_event.is_set():
            raise SideCaptureCancelled("Side capture was stopped")

    def wait(seconds):
        if cancel_event.wait(seconds):
            check_cancelled()

    def command(text, expected, timeout=5.0):
        check_cancelled()
        return motor.send_and_wait(
            text, expected, timeout=timeout, cancel_event=cancel_event,
        )

    def fresh_frame(after_id=None, not_before=0.0):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            check_cancelled()
            packet = camera.get_frame_packet(
                after_id=after_id, timeout=0.2, copy=True,
            )
            if packet is None:
                continue
            after_id, received_at, frame = packet
            if received_at >= not_before:
                return after_id, received_at, frame
        raise RuntimeError("No new side-camera frame arrived after settling")

    try:
        directory.mkdir(parents=True, exist_ok=False)
        # Validate that the camera supplies frames before moving the objective.
        fresh_frame()
        command("STOP OBJ", "OK STOP OBJ")
        command("MICRO 1", "OK MICRO 1")
        command(f"OBJ SPEED {speed}", "OK OBJ SPEED")
        command("OBJ ENABLE", "OK OBJ ENABLED")
        wait(0.2)
        if progress:
            progress(SideCaptureProgress(0, total, 0, directory))

        with (directory / "capture.csv").open(
            "w", newline="", encoding="utf-8", buffering=1,
        ) as output:
            writer = csv.writer(output)
            writer.writerow((
                "index", "commanded_full_steps", "firmware_position",
                "microstep", "speed_pulses_per_s", "settle_seconds",
                "frame_id", "frame_received_monotonic", "saved_at",
                "width", "height", "file",
            ))
            for index in range(1, total + 1):
                done = command(
                    f"OBJ MOVE {step}", "DONE OBJ POS ",
                    timeout=max(5.0, step / speed + 3.0),
                )
                firmware_position = int(done.rsplit(" ", 1)[-1])
                wait(settle_seconds)
                # Discard the latest cached frame, then wait for another frame
                # received after settling. The driver may still add latency;
                # the CSV reports receive time, not sensor exposure time.
                latest = camera.get_frame_packet(wait=False, copy=False)
                after_id = latest[0] if latest else None
                frame_id, received_at, frame = fresh_frame(
                    after_id=after_id, not_before=time.monotonic(),
                )
                check_cancelled()
                commanded = index * step
                filename = f"side_{index:04d}_step_{commanded:04d}.png"
                ok, encoded = cv2.imencode(
                    ".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 1],
                )
                if not ok:
                    raise RuntimeError(f"Could not encode {filename}")
                # Finish writing each image before starting the next movement.
                encoded.tofile(str(directory / filename))
                height, width = frame.shape[:2]
                writer.writerow((
                    index, commanded, firmware_position, 1, speed,
                    settle_seconds, frame_id, received_at,
                    datetime.now().astimezone().isoformat(),
                    width, height, filename,
                ))
                if progress:
                    progress(SideCaptureProgress(index, total, commanded, directory))
        return directory
    finally:
        # Attempt disable even if STOP fails after a USB/serial disconnect.
        try:
            motor.objective_stop()
        finally:
            motor.objective_disable()
