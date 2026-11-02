"""Offline policy checks; no camera, motor commands, or mechanical simulation."""
import csv
from pathlib import Path
import unittest

from aruco_objective_switch import (
    CalibrationTrend, CorrectionBudget, CrossingProgress, SwitchSettings,
    adjacent_objective, full_steps_to_pulses, select_objective, within_tolerance,
)


class CalibrationPolicyTests(unittest.TestCase):
    def test_recorded_displaced_source_is_not_replaced_by_closer_neighbor(self):
        path = (Path(__file__).resolve().parent / 'objective_switch_logs' /
                'switch_20261021_214510_189215400.csv')
        if not path.is_file():
            self.skipTest('Recorded ambiguous source frame not present')
        with path.open(newline='', encoding='utf-8') as stream:
            markers = [{'id': int(r['detected_id']), 'dx_px': float(r['dx_px'])}
                       for r in csv.DictReader(stream) if r['stage'] == 'identify']
        self.assertEqual(select_objective(markers)['id'], 1)  # Old auto selection.
        source = select_objective(markers, expected_id=0)['id']
        self.assertEqual(source, 0)
        self.assertEqual(adjacent_objective(source, 1), 1)

    def test_recorded_target_is_selected_when_next_neighbor_is_more_central(self):
        import json
        path = Path(__file__).resolve().parent / 'scan_position_results' / 'detections.json'
        if not path.is_file():
            self.skipTest('Recorded image detections not present')
        frames = json.loads(path.read_text(encoding='utf-8'))['results']
        for frame in frames:
            markers = frame['markers']
            ids = {m['id'] for m in markers}
            if {1, 2} <= ids and select_objective(markers)['id'] == 2:
                target = select_objective(markers, expected_id=1)
                self.assertEqual(target['id'], 1)
                self.assertGreater(abs(target['dx_px']),
                                   abs(select_objective(markers)['dx_px']))
                return
        self.fail('Expected recorded multi-marker frame not found')

    def test_missing_locked_target_never_falls_back_to_visible_neighbor(self):
        with self.assertRaisesRegex(LookupError, 'Locked objective ID 1 is not visible'):
            select_objective([{'id': 2, 'dx_px': 0.1}], expected_id=1)

    def test_next_previous_targets_wrap_by_id(self):
        self.assertEqual(adjacent_objective(0, 1), 1)
        self.assertEqual(adjacent_objective(0, -1), 3)
        self.assertEqual(adjacent_objective(3, 1), 0)

    def test_recorded_recovery_can_continue_past_40_full_steps(self):
        path = (Path(__file__).resolve().parent / 'objective_switch_logs' /
                'switch_20261021_213013_521538400.csv')
        if not path.is_file():
            self.skipTest('Recorded recovery log not present')
        with path.open(newline='', encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream))
        initial = next(r for r in rows if r['stage'] == 'enabled_position')
        trend = CalibrationTrend(float(initial['dx_px']), 0.5)
        budget = CorrectionBudget(24, 40)
        travel = moves = 0
        for row in rows:
            if row['stage'] != 'corrected_position':
                continue
            next_travel = abs(float(row['commanded_full_steps']))
            budget.reserve(moves, travel, next_travel - travel, advancing=trend.advancing)
            travel = next_travel
            moves += 1
            trend.observe(float(row['dx_px']))
        self.assertEqual(travel, 40)
        self.assertGreater(trend.best_error, 5)
        message = budget.reserve(moves, travel, 4, advancing=trend.advancing)
        self.assertIn('budget extended', message)
        self.assertEqual(budget.step_limit, 60)

    def test_command_budget_renews_with_progress(self):
        budget = CorrectionBudget(24, 40)
        self.assertIsNotNone(budget.reserve(24, 10, 0.25, advancing=True))
        self.assertEqual(budget.move_limit, 36)

    def test_budget_is_not_renewed_without_progress(self):
        budget = CorrectionBudget(24, 40)
        with self.assertRaisesRegex(RuntimeError, 'without recent pixel improvement'):
            budget.reserve(24, 40, 0.25, advancing=False)

    def test_recent_improvement_expires_when_error_increases(self):
        trend = CalibrationTrend(30, 0.5)
        for error in (25, 20, 15):
            trend.observe(error)
        self.assertTrue(trend.advancing)
        for error in (16, 17, 18):
            trend.observe(error)
        self.assertFalse(trend.advancing)
        self.assertEqual(trend.stagnant, 3)

    def test_small_cumulative_improvements_reset_no_progress(self):
        trend = CalibrationTrend(10, 0.5)
        trend.observe(9.8)
        trend.observe(9.6)
        self.assertTrue(trend.observe(9.4))
        self.assertEqual(trend.stagnant, 0)

    def test_more_than_six_shrinking_crossings_are_allowed(self):
        crossing = CrossingProgress(0.5)
        errors = (-40, 35, -30, 25, -20, 15, -10, 8, -6, 5.5)
        for before, after in zip(errors, errors[1:]):
            self.assertIsNotNone(crossing.observe(before, after))
            self.assertEqual(crossing.stagnant, 0)
        self.assertTrue(crossing.advancing)

    def test_repeated_unshrinking_crossings_stop(self):
        crossing = CrossingProgress(0.5)
        for before, after in ((-10, 10), (10, -10), (-10, 10), (10, -10)):
            crossing.observe(before, after)
        self.assertEqual(crossing.stagnant, 3)
        self.assertFalse(crossing.advancing)

    def test_release_progress_can_outlast_initial_four_checks(self):
        trend = CalibrationTrend(30, 0.5)
        for error in (20, 15, 10, 7, 5.8):
            trend.observe(error)
            self.assertTrue(trend.advancing)
            self.assertEqual(trend.stagnant, 0)

    def test_repeated_release_error_is_not_progress(self):
        trend = CalibrationTrend(10, 0.5)
        for error in (10.1, 9.9, 10.0):
            trend.observe(error)
        self.assertEqual(trend.stagnant, 3)
        self.assertFalse(trend.advancing)

    def test_tolerance_microstep_and_speed_contract_unchanged(self):
        settings = SwitchSettings()
        self.assertEqual(settings.tolerance_px, 5)
        self.assertEqual(full_steps_to_pulses(185), 740)
        self.assertEqual(settings.speed * 4, 800)
        self.assertFalse(within_tolerance([{'dx_px': 4.9}, {'dx_px': 5.1}], 5))
        self.assertTrue(within_tolerance([{'dx_px': -5}, {'dx_px': 5}], 5))


if __name__ == '__main__':
    unittest.main()
