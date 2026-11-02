import unittest
from unittest.mock import Mock
from autofocus import AutoFocuser, FocusMeasurement

class FocusCompletionTest(unittest.TestCase):
    def test_selects_actual_maximum_on_near_flat_peak(self):
        samples=[FocusMeasurement(0,100,'fine'),FocusMeasurement(10,99.9,'fine'),
                 FocusMeasurement(20,99.8,'fine')]
        self.assertEqual(AutoFocuser._fine_candidate(samples).position,0)

    def test_lower_final_score_is_recorded_without_correction(self):
        focuser=object.__new__(AutoFocuser)
        focuser._return_to_best=Mock()
        final=FocusMeasurement(50,5.218,'local_final')
        focuser._measure=Mock(return_value=final)
        focuser._adaptive_fine_scan=Mock(side_effect=AssertionError('Unexpected rescan'))
        result=focuser._land_and_verify(FocusMeasurement(50,5.591,'local_fine'),
            lower_limit=-1500,upper_limit=1500,correction_radius=100,min_step=10,
            stage_prefix='local_')
        self.assertIs(result,final)
        focuser._return_to_best.assert_called_once_with(50,-1500)
        focuser._measure.assert_called_once_with('local_final',precision=True)
        focuser._adaptive_fine_scan.assert_not_called()

if __name__=='__main__': unittest.main()
