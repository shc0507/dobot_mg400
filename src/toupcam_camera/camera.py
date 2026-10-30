"""Reusable Toupcam microscope feed, stabilization, snapshots, and recording."""

import csv
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .capture import ToupcamCapture, selection


# Owns one recording run, including video segments and synchronized metadata.
class _RecordingSession:
    """Write bounded video segments and stream timestamps to disk."""

    # Open the timestamp CSV and first video segment for one recording session.
    def __init__(
        self,
        path,
        frame_size,
        fps,
        codec,
        timestamps_path,
        segment_minutes,
        minimum_free_gb,
        metadata_provider,
        metadata_fields,
        capture_interval_seconds=None,
    ):
        self.output_path = Path(path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.frame_size = frame_size
        self.fps = float(fps)
        self.codec = codec
        self.segment_seconds = None if segment_minutes is None else float(segment_minutes) * 60
        self.capture_interval_seconds = (
            None if capture_interval_seconds is None else float(capture_interval_seconds)
        )
        self.minimum_free_bytes = (
            None if minimum_free_gb is None else int(float(minimum_free_gb) * 1024**3)
        )
        self.metadata_provider = metadata_provider
        self.metadata_fields = tuple(metadata_fields or ())
        self.timestamps_path = Path(timestamps_path)
        self.timestamps_path.parent.mkdir(parents=True, exist_ok=True)
        self.segments = []
        self.segment_index = 0
        self.segment_frame = 0
        self.total_frame = 0
        self.segment_started_at = None
        self._last_disk_check = 0.0
        self._writer = None
        self._last_captured_at = None
        # Line buffering keeps long-run metadata durable without flushing each frame.
        self._timestamp_file = self.timestamps_path.open(
            "w", newline="", encoding="utf-8", buffering=1
        )
        self._csv = csv.writer(self._timestamp_file)
        self._csv.writerow(
            ("frame_index", "segment_index", "segment_frame", "unix_time", *self.metadata_fields)
        )
        try:
            self._open_segment(time.time())
        except Exception:
            self._timestamp_file.close()
            raise

    # Choose the base filename or the next numbered filename for segmented output.
    def _next_path(self):
        if self.segment_seconds is None:
            return self.output_path
        return self.output_path.with_name(
            f"{self.output_path.stem}_{self.segment_index + 1:04d}{self.output_path.suffix}"
        )

    # Start a new segment so very long recordings do not depend on one huge file.
    # Create and validate an OpenCV writer for the next video segment.
    def _open_segment(self, started_at):
        path = self._next_path()
        writer = cv2.VideoWriter(
            str(path), cv2.VideoWriter_fourcc(*self.codec), self.fps, self.frame_size
        )
        if not writer.isOpened():
            writer.release()
            raise RuntimeError(f"Could not open video for writing: {path}")
        self._writer = writer
        self.segment_started_at = started_at
        self.segment_frame = 0
        self.segments.append(path)

    # Write one frame plus its timestamp and matching furnace metadata row.
    def write(self, frame, captured_at):
        if self.capture_interval_seconds is not None:
            should_write = (
                self._last_captured_at is None
                or captured_at - self._last_captured_at >= self.capture_interval_seconds
            )
            if not should_write:
                return
            self._last_captured_at = captured_at
        if (
            self.segment_seconds is not None
            and captured_at - self.segment_started_at >= self.segment_seconds
        ):
            self._writer.release()
            self.segment_index += 1
            self._open_segment(captured_at)
        if self.minimum_free_bytes is not None and captured_at - self._last_disk_check >= 10:
            self._last_disk_check = captured_at
            free = shutil.disk_usage(self.output_path.parent).free
            if free < self.minimum_free_bytes:
                raise RuntimeError(
                    f"Recording stopped: less than {self.minimum_free_bytes / 1024**3:.1f} GB free"
                )
        self._writer.write(frame)
        metadata = self.metadata_provider() if self.metadata_provider else {}
        self._csv.writerow(
            (
                self.total_frame,
                self.segment_index,
                self.segment_frame,
                captured_at,
                *(metadata.get(field) for field in self.metadata_fields),
            )
        )
        self.total_frame += 1
        self.segment_frame += 1

    # Finalize the writer and CSV; repeated calls are harmless.
    def close(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        if self._timestamp_file is not None:
            self._timestamp_file.flush()
            self._timestamp_file.close()
            self._timestamp_file = None


# GUI-independent camera API used by Flask now and reusable by a future desktop UI.
class MicroscopeCamera:
    """Threaded camera feed with optional stabilization and safe recording."""

    # Configure camera, stabilization, frame sharing, and recording state.
    def __init__(
        self,
        camera_index=None,
        resolution=(2592, 1944),
        stabilization=True,
        smoothing=0.9,
        *,
        camera_serial=None,
        settings=None,
        capture_factory=None,
    ):
        if not 0 <= smoothing < 1:
            raise ValueError("smoothing must be at least 0 and less than 1")
        self.camera_index, self.camera_serial = selection(
            camera_index, camera_serial, settings=settings
        )
        self._capture_factory = capture_factory or ToupcamCapture
        self.resolution = resolution
        self.stabilization_enabled = stabilization
        self.smoothing = smoothing
        self._source = None
        self._thread = None
        self._lifecycle_lock = threading.RLock()
        self._recording_mode = "time_lapse"
        self._recording_interval_seconds = 30.0
        self._recording_fps = 1.0
        self._stop_event = threading.Event()
        # Consumers receive the latest frame; slow clients do not build a backlog.
        self._frame_condition = threading.Condition()
        # Recording state is shared by request and acquisition threads.
        self._recording_lock = threading.Lock()
        self._stabilization_lock = threading.Lock()
        self._latest_frame = None
        self._latest_raw_frame = None
        self._frame_id = 0
        self._captured_at = None
        self._error = None
        self._recording = None
        self._recording_error = None
        self._last_recording_segments = []
        self.reset_stabilization()

    # Camera lifecycle -----------------------------------------------------
    def open(self):
        """Open the camera and begin background acquisition, but not recording."""
        with self._lifecycle_lock:
            return self._open()

    def _open(self):
        if self.is_open:
            return self
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("The previous acquisition worker is still stopping.")
        self._error = None
        self._stop_event.clear()
        self.reset_stabilization()
        with self._frame_condition:
            self._latest_frame = self._latest_raw_frame = None
            self._frame_id = 0
            self._captured_at = None
        self._source = self._capture_factory(
            self.camera_index, self.resolution, camera_serial=self.camera_serial
        )
        self._thread = threading.Thread(
            target=self._acquisition_loop, name="microscope-camera", daemon=True
        )
        try:
            self._thread.start()
        except BaseException:
            source, self._source = self._source, None
            source.release()
            self._thread = None
            raise
        return self

    # Finalize the writer and CSV; repeated calls are harmless.
    def close(self):
        """Finalize recording and release the camera. Safe to call repeatedly."""
        with self._lifecycle_lock:
            self._close()

    def _close(self):
        self._stop_event.set()
        self.stop_recording()
        source, self._source = self._source, None
        if source is not None:
            source.release()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                raise RuntimeError(
                    "The acquisition worker is still stopping. Wait before reopening."
                )
            self._thread = None
        with self._frame_condition:
            self._latest_frame = None
            self._latest_raw_frame = None
            self._frame_condition.notify_all()

    # Support opening the camera with a with-statement.
    def __enter__(self):
        return self.open()

    # Ensure hardware and recording resources close when leaving a with-block.
    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @property
    # Indicate whether the Toupcam capture wrapper is currently allocated.
    def is_open(self):
        return self._source is not None

    @property
    # Read active recording state under its synchronization lock.
    def is_recording(self):
        with self._recording_lock:
            return self._recording is not None

    @property
    def recording_error(self):
        """Recording error, if recording stopped automatically."""
        return self._recording_error

    @property
    def recording_segments(self):
        """Files from the active or most recently stopped recording."""
        with self._recording_lock:
            if self._recording is not None:
                return list(self._recording.segments)
            return list(self._last_recording_segments)

    @property
    # Return the active camera dimensions, or None while closed.
    def frame_size(self):
        return None if self._source is None else (self._source.width, self._source.height)

    @property
    def recording_mode(self):
        return self._recording_mode

    @property
    def recording_interval_seconds(self):
        return self._recording_interval_seconds

    @property
    def recording_fps(self):
        return self._recording_fps

    @property
    # Expose the low-level Toupcam object for advanced camera settings.
    def sdk_camera(self):
        if self._source is None:
            raise RuntimeError("Camera is not open")
        return self._source.sdk_camera

    @property
    # Return the exception that stopped background acquisition, if one occurred.
    def last_error(self):
        return self._error

    def get_frame(self, wait=True, timeout=2.0, raw=False, copy=True):
        """Get the newest BGR frame; use wait=False from a GUI refresh timer."""
        if not self.is_open:
            raise RuntimeError("Camera is not open; call open() first")
        deadline = time.monotonic() + timeout
        with self._frame_condition:
            frame = self._latest_raw_frame if raw else self._latest_frame
            while frame is None and wait and self._error is None and not self._stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._frame_condition.wait(remaining)
                frame = self._latest_raw_frame if raw else self._latest_frame
            if self._error is not None:
                raise RuntimeError("Camera acquisition stopped") from self._error
            return None if frame is None else (frame.copy() if copy else frame)

    def get_frame_packet(self, after_id=None, wait=True, timeout=2.0, raw=False, copy=True):
        """Return ``(frame_id, captured_at, frame)`` for a new camera frame.

        Pass the previously received frame ID as ``after_id`` to wait for a
        genuinely newer frame instead of scoring the cached frame again.
        """
        if not self.is_open:
            raise RuntimeError("Camera is not open; call open() first")
        deadline = time.monotonic() + timeout
        with self._frame_condition:
            while self._error is None and not self._stop_event.is_set():
                frame = self._latest_raw_frame if raw else self._latest_frame
                is_new = after_id is None or self._frame_id > after_id
                if frame is not None and is_new:
                    break
                if not wait:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._frame_condition.wait(remaining)
            if self._error is not None:
                raise RuntimeError("Camera acquisition stopped") from self._error
            frame = self._latest_raw_frame if raw else self._latest_frame
            if frame is None or (after_id is not None and self._frame_id <= after_id):
                return None
            return (
                self._frame_id,
                self._captured_at,
                frame.copy() if copy else frame,
            )

    # Recording and snapshots ----------------------------------------------
    def start_recording(
        self,
        path="capture.mp4",
        fps=30.0,
        codec="mp4v",
        timestamps_path=None,
        segment_minutes=60,
        minimum_free_gb=2.0,
        metadata_provider=None,
        metadata_fields=None,
        mode="time_lapse",
        capture_interval_seconds=None,
        output_fps=None,
    ):
        """Start recording; one-hour segments are used by default.

        Set mode="time_lapse" to record only every N seconds while keeping the live
        preview running in real time.
        """
        with self._lifecycle_lock:
            if not self.is_open:
                raise RuntimeError("Camera is not open; call open() first")
            if mode not in {"live", "time_lapse"}:
                raise ValueError("mode must be 'live' or 'time_lapse'")
            if mode == "time_lapse":
                if capture_interval_seconds is None:
                    capture_interval_seconds = 30.0
                if capture_interval_seconds <= 0:
                    raise ValueError("capture_interval_seconds must be positive")
                if output_fps is None:
                    output_fps = 1.0
                effective_fps = float(output_fps)
                effective_capture_interval = float(capture_interval_seconds)
            else:
                effective_fps = float(fps)
                effective_capture_interval = None
            if effective_fps <= 0:
                raise ValueError("fps must be greater than zero")
            if len(codec) != 4:
                raise ValueError("codec must contain exactly four characters")
            if segment_minutes is not None and segment_minutes <= 0:
                raise ValueError("segment_minutes must be positive or None")
            if minimum_free_gb is not None and minimum_free_gb < 0:
                raise ValueError("minimum_free_gb cannot be negative")
            frame = self.get_frame(wait=True, timeout=3.0, copy=False)
            if frame is None:
                raise RuntimeError("No camera frame is available")
            path = Path(path)
            timestamps_path = (
                Path(timestamps_path)
                if timestamps_path is not None
                else path.with_suffix(".timestamps.csv")
            )
            height, width = frame.shape[:2]
            with self._recording_lock:
                if self._recording is not None:
                    raise RuntimeError("Recording is already in progress")
                session = _RecordingSession(
                    path,
                    (width, height),
                    effective_fps,
                    codec,
                    timestamps_path,
                    segment_minutes,
                    minimum_free_gb,
                    metadata_provider,
                    metadata_fields,
                    effective_capture_interval,
                )
                self._recording = session
                self._recording_error = None
                self._last_recording_segments = []
                self._recording_mode = mode
                self._recording_interval_seconds = effective_capture_interval
                self._recording_fps = effective_fps
            return path

    def stop_recording(self):
        """Finalize the current segment and return the requested base path."""
        with self._lifecycle_lock:
            with self._recording_lock:
                session, self._recording = self._recording, None
            if session is None:
                return None
            session.close()
            with self._recording_lock:
                self._last_recording_segments = list(session.segments)
            return session.output_path

    # Save either the stabilized or raw latest frame as an image file.
    def save_snapshot(self, path="snapshot.png", raw=False):
        frame = self.get_frame(wait=True, timeout=3.0, raw=raw)
        if frame is None:
            raise RuntimeError("No camera frame is available")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), frame):
            raise RuntimeError(f"Could not write snapshot: {path}")
        return path

    # Optional image stabilization and camera controls ---------------------
    # Toggle stabilization and reset its accumulated motion estimate.
    def set_stabilization(self, enabled):
        with self._stabilization_lock:
            self.stabilization_enabled = bool(enabled)
            self._reset_stabilization_unlocked()

    # Safely clear stabilization history while acquisition may be running.
    def reset_stabilization(self):
        with self._stabilization_lock:
            self._reset_stabilization_unlocked()

    # Initialize center-lock tracking state; caller holds the stabilization lock.
    def _reset_stabilization_unlocked(self):
        self._previous_gray = None
        self._tracking_points = None
        self._tracked_center = None
        self._cumulative_motion = np.zeros(2, dtype=np.float32)
        self._smoothed_correction = np.zeros(2, dtype=np.float32)

    # Find strong tracking points in a box centered on the specimen's expected position.
    def _find_tracking_points(self, gray, center):
        height, width = gray.shape
        box_size = max(64, int(min(width, height) * 0.22))
        half = box_size // 2
        center_x = int(np.clip(center[0], half, width - half - 1))
        center_y = int(np.clip(center[1], half, height - half - 1))
        x1, y1 = center_x - half, center_y - half
        x2, y2 = center_x + half, center_y + half
        points = cv2.goodFeaturesToTrack(
            gray[y1:y2, x1:x2],
            maxCorners=120,
            qualityLevel=0.01,
            minDistance=12,
            blockSize=7,
        )
        if points is None:
            return None
        points[:, 0, 0] += x1
        points[:, 0, 1] += y1
        return points.astype(np.float32)

    # Enable or disable the camera SDK automatic exposure algorithm.
    def set_auto_exposure(self, enabled=True):
        self.sdk_camera.put_AutoExpoEnable(1 if enabled else 0)

    # Set manual exposure time in microseconds through the camera SDK.
    def set_exposure(self, microseconds):
        self.sdk_camera.put_ExpoTime(int(microseconds))

    # Read the current exposure time from the camera SDK.
    def get_exposure(self):
        return self.sdk_camera.get_ExpoTime()

    # Set the camera analog gain through the SDK.
    def set_gain(self, gain):
        self.sdk_camera.put_ExpoAGain(int(gain))

    # Read the current analog gain from the camera SDK.
    def get_gain(self):
        return self.sdk_camera.get_ExpoAGain()

    # Ask the camera SDK to perform one automatic white-balance operation.
    def auto_white_balance(self):
        self.sdk_camera.AwbOnce()

    # Acquisition runs independently of web requests and preview clients.
    # Continuously acquire frames, stabilize them, publish them, and record if active.
    def _acquisition_loop(self):
        try:
            while not self._stop_event.is_set():
                source = self._source
                if source is None:
                    break
                success, raw = source.read(timeout=0.5)
                if not success or self._stop_event.is_set() or source is not self._source:
                    continue
                with self._stabilization_lock:
                    frame = self._stabilize(raw) if self.stabilization_enabled else raw.copy()
                captured_at = time.time()
                with self._frame_condition:
                    self._latest_raw_frame = raw
                    self._latest_frame = frame
                    self._frame_id += 1
                    self._captured_at = captured_at
                    self._frame_condition.notify_all()
                with self._recording_lock:
                    session = self._recording
                    if session is not None:
                        try:
                            # Recording is independent of preview traffic.
                            session.write(frame, captured_at)
                        except Exception as exc:
                            self._recording = None
                            self._recording_error = exc
                            self._last_recording_segments = list(session.segments)
                            session.close()
        except Exception as exc:
            if not self._stop_event.is_set():
                self._error = exc
                # close() can hold the lifecycle lock while joining this worker.
                # Finalize under the recording lock without acquiring the lifecycle lock.
                with self._recording_lock:
                    session, self._recording = self._recording, None
                    if session is not None:
                        self._recording_error = exc
                        self._last_recording_segments = list(session.segments)
                        try:
                            session.close()
                        except Exception as cleanup_error:
                            self._recording_error = cleanup_error
                with self._frame_condition:
                    self._frame_condition.notify_all()

    # Keep the specimen selected by the center box at the center of the output frame.
    def _stabilize(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height, width = gray.shape
        frame_center = np.array((width / 2.0, height / 2.0), dtype=np.float32)

        if self._previous_gray is None:
            self._previous_gray = gray
            self._tracked_center = frame_center.copy()
            self._tracking_points = self._find_tracking_points(gray, frame_center)
            return frame.copy()

        if self._tracking_points is None or len(self._tracking_points) < 6:
            expected_center = frame_center + self._cumulative_motion
            self._tracking_points = self._find_tracking_points(self._previous_gray, expected_center)

        if self._tracking_points is None or len(self._tracking_points) < 6:
            # With no reliable subject features, show the raw frame and reacquire next time.
            self._reset_stabilization_unlocked()
            self._previous_gray = gray
            return frame.copy()

        current, forward_status, _ = cv2.calcOpticalFlowPyrLK(
            self._previous_gray,
            gray,
            self._tracking_points,
            None,
            winSize=(31, 31),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        )
        backward, backward_status, _ = (
            cv2.calcOpticalFlowPyrLK(
                gray,
                self._previous_gray,
                current,
                None,
                winSize=(31, 31),
                maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
            )
            if current is not None
            else (None, None, None)
        )

        self._previous_gray = gray
        if current is None or backward is None:
            self._tracking_points = None
            return frame.copy()

        forward_ok = forward_status.reshape(-1).astype(bool)
        backward_ok = backward_status.reshape(-1).astype(bool)
        round_trip_error = np.linalg.norm(
            self._tracking_points.reshape(-1, 2) - backward.reshape(-1, 2), axis=1
        )
        valid = forward_ok & backward_ok & (round_trip_error < 2.0)
        previous_good = self._tracking_points.reshape(-1, 2)[valid]
        current_good = current.reshape(-1, 2)[valid]

        if len(current_good) < 6:
            self._tracking_points = None
            return frame.copy()

        displacements = current_good - previous_good
        median_displacement = np.median(displacements, axis=0)
        residuals = np.linalg.norm(displacements - median_displacement, axis=1)
        median_residual = np.median(residuals)
        inliers = residuals <= max(2.0, 3.0 * median_residual)
        current_good = current_good[inliers]

        max_step = max(10.0, min(width, height) * 0.03)
        if len(current_good) < 6 or np.linalg.norm(median_displacement) > max_step:
            # A sudden implausible jump means tracking was lost; never accumulate it.
            self._tracking_points = None
            return frame.copy()

        self._cumulative_motion += median_displacement.astype(np.float32)
        max_offset = np.array((width * 0.25, height * 0.25), dtype=np.float32)
        self._cumulative_motion = np.clip(self._cumulative_motion, -max_offset, max_offset)
        target_correction = -self._cumulative_motion
        self._smoothed_correction = (
            self.smoothing * self._smoothed_correction + (1.0 - self.smoothing) * target_correction
        )
        self._tracked_center = frame_center + self._cumulative_motion
        self._tracking_points = current_good.reshape(-1, 1, 2).astype(np.float32)

        if len(self._tracking_points) < 30:
            new_points = self._find_tracking_points(gray, self._tracked_center)
            if new_points is not None:
                self._tracking_points = np.concatenate((self._tracking_points, new_points), axis=0)[
                    :120
                ]

        correction = np.array(
            [[1.0, 0.0, self._smoothed_correction[0]], [0.0, 1.0, self._smoothed_correction[1]]],
            dtype=np.float32,
        )
        # Reflection avoids black borders; the 25% clamp prevents runaway transforms.
        return cv2.warpAffine(
            frame,
            correction,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )
