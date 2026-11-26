import unittest
from unittest.mock import Mock

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

    def test_ramped_coarse_scan_waits_for_known_positions(self):
        focuser = object.__new__(AutoFocuser)
        focuser.motor = Mock(ramped_motion=True)
        focuser._scan = Mock(return_value=[FocusMeasurement(0, 10, "coarse")])
        focuser._continuous_scan = Mock(side_effect=AssertionError("No timed interpolation"))
        result = focuser._coarse_scan(800, 200, "coarse")
        focuser._scan.assert_called_once_with(-800, 800, 100, "coarse")
        self.assertEqual(result[0].score, 10)

    def test_final_approach_matches_negative_fine_scan(self):
        focuser = object.__new__(AutoFocuser)
        focuser.position = -50
        focuser.backlash_steps = 200
        focuser._move_to = Mock()
        focuser._return_to_best(50, 100)
        self.assertEqual([call.args[0] for call in focuser._move_to.call_args_list], [100, 50])

    def test_poor_landing_gets_one_bounded_refinement(self):
        focuser = object.__new__(AutoFocuser)
        focuser._return_to_best = Mock()
        focuser._measure = Mock(
            side_effect=[
                FocusMeasurement(50, 3, "final"),
                FocusMeasurement(60, 9.5, "verified_final"),
            ]
        )
        focuser._adaptive_fine_scan = Mock(
            return_value=[FocusMeasurement(60, 10, "landing_refine")]
        )
        result = focuser._land_and_verify(
            FocusMeasurement(50, 10, "fine"),
            lower_limit=0,
            upper_limit=100,
            correction_radius=20,
            min_step=5,
        )
        self.assertEqual(result.score, 9.5)
        self.assertEqual(focuser._adaptive_fine_scan.call_count, 1)
        self.assertEqual(focuser._adaptive_fine_scan.call_args.kwargs["lower_limit"], 30)
        self.assertEqual(focuser._adaptive_fine_scan.call_args.kwargs["start"], 70)

    def test_failed_refinement_reports_failure_without_looping(self):
        focuser = object.__new__(AutoFocuser)
        focuser._return_to_best = Mock()
        focuser._measure = Mock(return_value=FocusMeasurement(50, 3, "final"))
        focuser._adaptive_fine_scan = Mock(
            return_value=[FocusMeasurement(50, 10, "landing_refine")]
        )
        with self.assertRaisesRegex(NoFocusPeak, "after 1 refinement"):
            focuser._land_and_verify(
                FocusMeasurement(50, 10, "fine"),
                lower_limit=0,
                upper_limit=100,
                correction_radius=20,
                min_step=5,
            )
        self.assertEqual(focuser._adaptive_fine_scan.call_count, 1)


if __name__ == "__main__":
    unittest.main()
