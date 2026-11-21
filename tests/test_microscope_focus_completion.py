import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from microscope.autofocus import AutoFocuser, FocusMeasurement, NoFocusPeak


class FocusCompletionTest(unittest.TestCase):
    def test_selects_actual_maximum_on_near_flat_peak(self):
        samples = [
            FocusMeasurement(0, 100, "fine"),
            FocusMeasurement(10, 99.9, "fine"),
            FocusMeasurement(20, 99.8, "fine"),
        ]
        self.assertEqual(AutoFocuser._fine_candidate(samples).position, 0)

    def test_lower_final_score_is_recorded_without_correction(self):
        focuser = object.__new__(AutoFocuser)
        focuser._return_to_best = Mock()
        final = FocusMeasurement(50, 5.218, "local_final")
        focuser._measure = Mock(return_value=final)
        focuser._adaptive_fine_scan = Mock(side_effect=AssertionError("Unexpected rescan"))
        result = focuser._land_and_verify(
            FocusMeasurement(50, 5.591, "local_fine"),
            lower_limit=-1500,
            upper_limit=1500,
            correction_radius=100,
            min_step=10,
            stage_prefix="local_",
        )
        self.assertIs(result, final)
        focuser._return_to_best.assert_called_once_with(50, 1500)
        focuser._measure.assert_called_once_with("local_final", precision=True)
        focuser._adaptive_fine_scan.assert_not_called()


class BacklashFocus:
    """Focus drive with a dead band: the lens lags `backlash` pulses on upward travel."""

    connected = True

    def __init__(self, backlash=0, speed=100_000):
        self.backlash, self.speed = backlash, speed
        self.commanded = self.lens = 0.0
        self._motion = None

    def _settle(self, commanded):
        self.commanded = commanded
        self.lens = min(max(self.lens, commanded - self.backlash), commanded)

    def lens_now(self):
        if self._motion is not None:
            start, distance, began = self._motion
            fraction = min(1.0, (time.monotonic() - began) * self.speed / abs(distance))
            self._settle(start + distance * fraction)
        return self.lens

    def focus_move(self, steps, *, cancel_event=None):
        self._settle(self.commanded + steps)

    def focus_move_async(self, steps, *, cancel_event=None):
        self._motion = (self.commanded, steps, time.monotonic())
        return SimpleNamespace(started=self._motion[2], started_wall=time.time())

    def wait_focus_done(self, ticket, *, cancel_event=None):
        start, distance, _ = self._motion
        self._motion = None
        self._settle(start + distance)

    def focus_speed(self, speed):
        pass

    def focus_enable(self):
        pass

    def focus_stop(self):
        pass


class CurveCamera:
    """Camera whose frame carries the lens position; `curve` gives its score."""

    is_open = True

    def __init__(self, motor, curve, *, recording=False):
        self.motor, self.curve, self.is_recording = motor, curve, recording
        self.stabilization_enabled = True
        self.stabilization_calls = []
        self.frame_id = 0

    def get_frame_packet(self, *, after_id=None, wait=True, timeout=None, raw=False, copy=False):
        self.frame_id += 1
        return self.frame_id, time.time(), np.array([self.motor.lens_now()])

    def set_stabilization(self, enabled):
        self.stabilization_calls.append(enabled)
        self.stabilization_enabled = enabled


def peak(center, sigma=80.0):
    return lambda lens: 5.0 + 100.0 * math.exp(-((lens - center) ** 2) / (2 * sigma**2))


@pytest.fixture
def curve_score(monkeypatch):
    def install(camera):
        monkeypatch.setattr(
            "microscope.autofocus.focus_score", lambda frame, roi: camera.curve(float(frame[0]))
        )

    return install


def focuser_for(camera, motor, backlash_steps=200):
    return AutoFocuser(
        camera,
        motor,
        speed=motor.speed,
        settle_seconds=0,
        frames_per_position=1,
        precision_settle_seconds=0,
        precision_frames_per_position=1,
        backlash_steps=backlash_steps,
    )


def test_landing_repeats_the_fine_scan_direction_under_backlash(curve_score):
    motor = BacklashFocus(backlash=60)
    camera = CurveCamera(motor, peak(0))
    curve_score(camera)
    result = focuser_for(camera, motor).run_local(half_range=1500, step=100, lock_exposure=False)
    assert abs(motor.lens) <= 10
    assert result.score >= 104


def test_plateau_stop_needs_a_score_near_the_coarse_peak(curve_score):
    # The coarse estimate (0) is displaced from the narrow peak (-300).
    motor = BacklashFocus()
    camera = CurveCamera(motor, peak(-300, sigma=40))
    curve_score(camera)
    focuser = focuser_for(camera, motor)
    samples = focuser._adaptive_fine_scan(
        start=200,
        stop=-200,
        predicted_peak=0,
        max_step=100,
        min_step=10,
        lower_limit=-1500,
        coarse_score=105.0,
    )
    assert abs(focuser._fine_candidate(samples).position + 300) <= 10


def test_flat_focus_scores_report_no_peak(curve_score):
    motor = BacklashFocus()
    camera = CurveCamera(motor, lambda lens: 5.0)
    curve_score(camera)
    with pytest.raises(NoFocusPeak, match="flat"):
        focuser_for(camera, motor).run_local(half_range=1500, step=100, lock_exposure=False)


@pytest.mark.parametrize("recording, calls", [(False, [False, True]), (True, [])])
def test_autofocus_keeps_stabilization_while_recording(curve_score, recording, calls):
    motor = BacklashFocus()
    camera = CurveCamera(motor, peak(0), recording=recording)
    curve_score(camera)
    focuser_for(camera, motor).run_local(half_range=1500, step=100, lock_exposure=False)
    assert camera.stabilization_calls == calls


if __name__ == "__main__":
    unittest.main()
