"""Small artificial fixtures below are TEST DATA ONLY, never analytical evidence."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
import analyze


def fixture(**changes):
    row = dict(task_id='test-only-1', dispatch_time='2026-09-01T07:16:00+08:00',
               telemetry_synced_at='2026-08-31T23:14:00Z', plant_zone='test-zone',
               device_type='test-device', reported_health_score='0',
               continuous_run_hours='0', task_success='1')
    row.update(changes)
    return row


class DataContractTests(unittest.TestCase):
    def test_timezone_conversion(self):
        rows, quality, _ = analyze.prepare([fixture()])
        self.assertEqual(rows[0]['telemetry_age_minutes'], 2)
        self.assertEqual(rows[0]['dispatch_date'], '2026-09-01')
        self.assertEqual(quality['quarantined_unique_rows'], 0)

    def test_duplicate_removed_and_missing_not_zero(self):
        raw = fixture(task_success='')
        rows, quality, _ = analyze.prepare([raw, raw])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['success'])
        self.assertEqual(quality['missing_outcomes'], 1)
        self.assertEqual(quality['exact_duplicates_removed'], 1)
        with self.assertRaises(ValueError):
            analyze.evaluate(rows, analyze.Rule('all'))

    def test_conflicting_id_is_quarantined(self):
        rows, quality, _ = analyze.prepare([fixture(), fixture(task_success='0')])
        self.assertEqual(rows, [])
        self.assertEqual(quality['conflicting_task_ids'], 1)
        self.assertEqual(quality['quarantined_unique_rows'], 2)

    def test_bad_timezone_future_timestamp_and_bad_label(self):
        inputs = [fixture(task_id='test-only-a', dispatch_time='2026-09-01T07:16:00'),
                  fixture(task_id='test-only-b', telemetry_synced_at='2026-09-01T00:00:00Z'),
                  fixture(task_id='test-only-c', task_success='unknown')]
        rows, quality, _ = analyze.prepare(inputs)
        self.assertEqual(rows, [])
        self.assertEqual(quality['quarantined_unique_rows'], 3)

    def test_zero_is_valid(self):
        rows, _, _ = analyze.prepare([fixture()])
        self.assertEqual(rows[0]['reported_health_score'], 0)
        self.assertEqual(rows[0]['continuous_run_hours'], 0)


class PolicyTests(unittest.TestCase):
    def test_age_boundary_and_no_outcome_leakage(self):
        rule = analyze.Rule('age', 15)
        self.assertFalse(rule.selects({'telemetry_age_minutes': 14.999}))
        self.assertTrue(rule.selects({'telemetry_age_minutes': 15}))
        self.assertEqual(rule.selects({'telemetry_age_minutes': 20, 'success': 0}),
                         rule.selects({'telemetry_age_minutes': 20, 'success': 1}))

    def test_costs_hand_calculated(self):
        # Two observed failures, one in the two checked tasks: 240 baseline hours.
        rows = [{'telemetry_age_minutes': a, 'success': s} for a, s in [(20, 0), (21, 1), (5, 0), (1, 1)]]
        rule = analyze.Rule('age', 15)
        ideal = analyze.evaluate(rows, rule)
        self.assertEqual(ideal['baseline_hours'], 240)
        self.assertEqual(ideal['verification_hours'], 30)
        self.assertEqual(ideal['residual_wasted_hours'], 120)
        self.assertEqual(ideal['net_saved_hours'], 90)
        self.assertEqual(analyze.evaluate(rows, rule, .5)['net_saved_hours'], 30)
        self.assertEqual(analyze.evaluate(rows, rule, 0)['net_saved_hours'], -30)
        self.assertEqual(ideal['break_even_q'], .25)

    def test_missing_outcome_bounds_pay_for_all_checks(self):
        rows = [{'telemetry_age_minutes': 20, 'success': 0},
                {'telemetry_age_minutes': 20, 'success': None}]
        bounds = analyze.population_bounds(rows, analyze.Rule('age', 15))
        self.assertEqual(bounds['checks_including_unknown'], 2)
        self.assertEqual(bounds['net_saved_hours_bounds'], [90, 210])

    def test_rule_threshold_is_captured_per_instance(self):
        # Protect against the late-bound loop variable error in the JS exploration.
        rules = [analyze.Rule('age', t) for t in (10, 15, 20)]
        self.assertEqual([r.selects({'telemetry_age_minutes': 17}) for r in rules], [True, True, False])

    def test_complete_pipeline_on_supplied_data(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            metrics = analyze.analyze(analyze.ROOT/'data'/'maintenance_tasks.csv', Path(folder), '2026-09-05')
            self.assertEqual(metrics['quality']['raw_rows'], 1163)
            self.assertEqual(metrics['quality']['usable_unique_tasks'], 1108)
            self.assertEqual(metrics['quality']['labeled_tasks'], 1058)
            self.assertEqual(metrics['overall']['failures'], 153)
            self.assertEqual(metrics['selected_rule'], {'family': 'age', 'threshold': 15, 'zone': None, 'device': None})
            h = metrics['holdout']
            self.assertEqual((h['n'], h['checks'], h['caught_failures']), (474, 133, 48))
            self.assertEqual(h['total_hours'], 3795)
            self.assertEqual(h['net_saved_hours'], 3765)
            self.assertEqual(metrics['population_bounds_holdout']['net_saved_hours_bounds'], [3660, 4500])
            with (Path(folder)/'task_decisions.csv').open(encoding='utf-8') as handle:
                decisions = list(csv.DictReader(handle))
            self.assertEqual(len(decisions), 1108)
            self.assertEqual(len({r['task_id'] for r in decisions}), 1108)


if __name__ == '__main__':
    unittest.main()
