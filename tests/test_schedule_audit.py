import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.schedule_audit import audit, main

FIELDS = ['task_id','device_id','seq','priority','status','received_ns','enqueued_ns','started_ns','finished_ns']


def task(task_id, priority, enqueue, start, finish, device_id='esp32c3-01', seq=None):
    return dict(task_id=task_id, device_id=device_id, seq=task_id if seq is None else seq,
                priority=priority,status='completed',received_ns=enqueue,
                enqueued_ns=enqueue,started_ns=start,finished_ns=finish)


class ScheduleAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path=Path(self.temporary.name)/'tasks.csv'

    def write(self, rows):
        with self.path.open('w',encoding='utf-8',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=FIELDS)
            writer.writeheader();writer.writerows(rows)
        return self.path

    def test_pop_to_started_window_does_not_create_false_violation(self):
        rows=[task(1,'normal',1,10,20),task(2,'normal',2,30,40),task(3,'urgent',25,50,60)]
        result=audit(self.write(rows),'priority')
        self.assertTrue(result['policy_ok'])
        self.assertEqual(result['priority_violation_pairs'],0)
        self.assertFalse(result['priority_behavior_observed'])

    def test_urgent_overtakes_an_earlier_waiting_normal(self):
        rows=[task(1,'normal',1,10,20),task(2,'normal',2,50,60,'uno-01',0),task(3,'urgent',3,30,40,seq=0)]
        result=audit(self.write(rows),'priority')
        self.assertTrue(result['policy_ok'])
        self.assertEqual(result['urgent_overtake_decisions'],1)
        self.assertTrue(result['priority_behavior_observed'])
        sample=result['samples']['urgent_overtakes'][0]
        self.assertEqual(sample['selected']['seq'],0)
        self.assertEqual(sample['selected']['device_id'],'esp32c3-01')
        self.assertEqual(sample['waiting']['device_id'],'uno-01')
        self.assertEqual(sample['safe_boundary_ns'],20)

    def test_waiting_urgent_is_violation_only_for_priority(self):
        rows=[task(1,'normal',1,10,20),task(2,'normal',2,30,40),task(3,'urgent',3,50,60)]
        self.write(rows)
        priority=audit(self.path,'priority');fifo=audit(self.path,'fifo')
        self.assertFalse(priority['policy_ok'])
        self.assertEqual(priority['priority_violation_pairs'],1)
        self.assertTrue(fifo['policy_ok'])
        self.assertEqual(fifo['normal_with_waiting_urgent_pairs'],1)
        self.assertEqual(fifo['priority_violation_pairs'],0)

    def test_overlap_is_not_a_valid_single_worker_trace(self):
        rows=[task(1,'normal',1,10,35),task(2,'normal',2,30,40)]
        result=audit(self.write(rows),'fifo')
        self.assertEqual(result['nonpreemptive_overlap_count'],1)
        self.assertFalse(result['policy_ok'])
        self.assertEqual(result['audited_dispatches'],0)

    def test_same_priority_fifo_inversion_is_reported(self):
        rows=[task(1,'normal',1,10,20),task(3,'normal',3,30,40),task(2,'normal',2,50,60)]
        result=audit(self.write(rows),'priority')
        self.assertFalse(result['same_priority_fifo']['normal'])
        self.assertFalse(result['policy_ok'])
        self.assertEqual(len(result['samples']['same_priority_fifo_inversions']),1)

    def test_rows_are_sorted_by_start_not_csv_position(self):
        rows=[task(3,'urgent',3,50,60),task(1,'normal',1,10,20),task(2,'normal',2,30,40)]
        result=audit(self.write(rows),'fifo')
        self.assertTrue(result['policy_ok'])
        self.assertEqual(result['normal_with_waiting_urgent_pairs'],1)

    def test_ns_above_float_exact_range_stays_integer(self):
        base=2**60+1
        rows=[task(1,'normal',base,base+1,base+5),task(2,'normal',base+1,base+20,base+25),task(3,'urgent',base+2,base+10,base+15)]
        result=audit(self.write(rows),'priority')
        self.assertEqual(result['samples']['urgent_overtakes'][0]['safe_boundary_ns'],base+5)

    def test_bounded_samples_keep_full_pair_and_decision_counts(self):
        rows=[task(1,'normal',1,100,200)]
        rows.extend(task(i,'normal',i,300+(i-2)*100,350+(i-2)*100) for i in range(2,32))
        rows.extend(task(i,'urgent',i,4000+(i-32)*100,4050+(i-32)*100) for i in range(32,62))
        result=audit(self.write(rows),'priority')
        self.assertEqual(result['priority_violation_decisions'],30)
        self.assertEqual(result['priority_violation_pairs'],900)
        self.assertEqual(len(result['samples']['normal_with_waiting_urgent']),20)

    def test_first_dispatch_is_not_audited(self):
        result=audit(self.write([task(1,'normal',1,10,20)]),'priority')
        self.assertTrue(result['policy_ok'])
        self.assertTrue(result['first_dispatch_not_audited'])
        self.assertEqual(result['audited_dispatches'],0)
        self.assertFalse(result['priority_behavior_observed'])

    def test_fifo_rejects_out_of_order_cross_priority_dispatch(self):
        rows=[task(1,'normal',1,10,20),task(3,'urgent',3,30,40),task(2,'normal',2,50,60)]
        result=audit(self.write(rows),'fifo')
        self.assertFalse(result['fifo_dispatch_order'])
        self.assertFalse(result['policy_ok'])

    def test_invalid_rows_raise_instead_of_guessing(self):
        base=task(1,'normal',1,10,20)
        mutations=[{'started_ns':'1.0'},{'enqueued_ns':11},{'finished_ns':9},
                   {'received_ns':2},{'task_id':0},{'priority':'highest'},
                   {'status':'rejected'},{'seq':'NaN'},{'device_id':''},{'finished_ns':''}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):audit(self.write([dict(base,**mutation)]),'priority')

    def test_duplicate_task_id_is_invalid(self):
        with self.assertRaises(ValueError):
            audit(self.write([task(1,'normal',1,10,20),task(1,'urgent',2,30,40)]),'priority')

    def test_ambiguous_equal_starts_are_not_arbitrarily_ordered(self):
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            audit(self.write([task(1,'normal',1,10,10),task(2,'urgent',2,10,20)]),'priority')

    def test_empty_and_missing_columns_are_invalid(self):
        self.write([])
        with self.assertRaises(ValueError):audit(self.path,'fifo')
        self.path.write_text('device_id,seq\nuno-01,0\n',encoding='utf-8')
        with self.assertRaises(ValueError):audit(self.path,'fifo')

    def test_cli_requires_policy_and_returns_error_on_bad_csv(self):
        self.write([task(1,'normal',1,10,20)])
        with patch('sys.argv',['audit','--tasks',str(self.path),'--policy','priority']),contextlib.redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(),0)
        self.assertIn('"policy_ok": true',stream.getvalue())
        with patch('sys.argv',['audit','--tasks',str(self.path)]),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code,2)
        self.path.write_text('bad\n1\n',encoding='utf-8')
        with patch('sys.argv',['audit','--tasks',str(self.path),'--policy','fifo']),contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code,2)


if __name__=='__main__':
    unittest.main()
