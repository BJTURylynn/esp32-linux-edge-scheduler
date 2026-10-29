import csv
from pathlib import Path
import tempfile
import unittest

from tools.dispatch_verify import scheduling_audit, verify
from tools.protocol import reference


class DispatchVerifyTests(unittest.TestCase):
    def records(self, directory, corrupt=False):
        samples = [((index + 1) % 201) - 100 for index in range(16)]
        result = reference(samples, 8)
        rows = []
        for task_id, device, start in [(1, 'uno-01', 10), (2, 'esp32c3-01', 20)]:
            rows.append(dict(id=task_id, device=device, state='succeeded', seed=1, priority='normal',
                             count=16, window=8, passes=100, mean=result['mean'],
                             rms=result['rms'] + (1 if corrupt else 0), compute_us=123,
                             enqueued_ns=start-1, dispatched_ns=start, accepted_ns=start+1, finished_ns=100))
        with (directory/'tasks.csv').open('w', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)

    def test_two_device_results_verified_with_explicit_overlap_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.records(root)
            result = verify(root)
            self.assertTrue(result['numerical_verified'])
            self.assertTrue(result['all_tasks_succeeded'])
            self.assertTrue(result['inflight_overlap_observed'])
            self.assertTrue(result['policy_verified'])
            self.assertEqual(result['max_inflight'], 2)
            self.assertIn('not synchronized CPU', result['overlap_semantics'])

    def test_wrong_device_result_fails_independent_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.records(root, corrupt=True)
            with self.assertRaisesRegex(ValueError, 'independent reference'):
                verify(root)

    def test_missing_required_node_is_not_a_dual_device_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.records(root)
            with self.assertRaises(ValueError):
                verify(root, minimum=3)

    def test_priority_and_fifo_reconstructed_from_enqueue_not_finish_order(self):
        rows = [dict(id=1, priority='normal', enqueued_ns=1, dispatched_ns=20, finished_ns=30),
                dict(id=2, priority='urgent', enqueued_ns=2, dispatched_ns=10, finished_ns=15)]
        self.assertTrue(scheduling_audit(rows, 'priority')['policy_verified'])
        self.assertEqual(scheduling_audit(rows, 'priority')['urgent_overtake_decisions'], 1)
        self.assertFalse(scheduling_audit(rows, 'fifo')['policy_verified'])

    def test_same_class_fifo_violation_not_hidden_by_correct_priority(self):
        rows = [dict(id=1, priority='urgent', enqueued_ns=1, dispatched_ns=20, finished_ns=30),
                dict(id=2, priority='urgent', enqueued_ns=2, dispatched_ns=10, finished_ns=15)]
        self.assertFalse(scheduling_audit(rows)['policy_verified'])

    def test_two_simultaneous_jobs_on_same_device_fail_audit(self):
        rows = [dict(id=1, device='uno-01', priority='normal', enqueued_ns=1,
                     dispatched_ns=10, finished_ns=30),
                dict(id=2, device='uno-01', priority='normal', enqueued_ns=2,
                     dispatched_ns=20, finished_ns=40)]
        audit = scheduling_audit(rows)
        self.assertFalse(audit['policy_verified'])
        self.assertEqual(audit['per_device_max_inflight']['uno-01'], 2)


if __name__ == '__main__':
    unittest.main()
