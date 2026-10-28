"""Conservatively audit scheduling decisions from one C service tasks.csv.

started_ns is sampled AFTER dequeue/status publication/unlock. A task enqueued
before started_ns is not necessarily queued at dequeue. The previous worker
completion is the safe lower-bound witness for tasks already waiting.
"""
import argparse
import csv
import json
from pathlib import Path
import re

SAMPLE_LIMIT = 20
INTEGER_FIELDS = ('task_id', 'seq', 'received_ns', 'enqueued_ns', 'started_ns', 'finished_ns')
REQUIRED = {'device_id', 'priority', 'status', *INTEGER_FIELDS}


def _integer(value, field, line):
    if not isinstance(value, str) or re.fullmatch(r'[0-9]+', value) is None:
        raise ValueError(f'line {line}: {field} must be an unsigned decimal integer')
    return int(value)


def _read_tasks(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames
        if not header or len(header) != len(set(header)) or not REQUIRED <= set(header):
            raise ValueError('CSV needs unique columns and all required task/timestamp fields')
        rows = []
        ids = set()
        starts = set()
        for line, raw in enumerate(reader, 2):
            if None in raw or any(raw.get(key) is None for key in REQUIRED):
                raise ValueError(f'line {line}: incomplete or malformed CSV row')
            if raw['status'] != 'completed':
                raise ValueError(f'line {line}: audit requires completed rows, not {raw["status"]!r}')
            if raw['priority'] not in ('normal', 'urgent') or not raw['device_id']:
                raise ValueError(f'line {line}: invalid task identity or priority')
            row = {key: _integer(raw[key], key, line) for key in INTEGER_FIELDS}
            row.update(device_id=raw['device_id'], priority=raw['priority'])
            if row['task_id'] == 0 or row['task_id'] in ids:
                raise ValueError(f'line {line}: task_id must be positive and unique')
            if not row['received_ns'] <= row['enqueued_ns'] <= row['started_ns'] <= row['finished_ns']:
                raise ValueError(f'line {line}: invalid received/enqueued/started/finished ordering')
            # Equal starts cannot establish which dequeue occurred first. Never
            # manufacture a priority ordering from CSV position or task_id.
            if row['started_ns'] in starts:
                raise ValueError(f'line {line}: identical started_ns makes dispatch order ambiguous')
            ids.add(row['task_id'])
            starts.add(row['started_ns'])
            rows.append(row)
    if not rows:
        raise ValueError('no completed tasks to audit')
    return sorted(rows, key=lambda row: row['started_ns'])


def _identity(row):
    return {key: row[key] for key in ('task_id', 'device_id', 'seq', 'priority',
                                      'enqueued_ns', 'started_ns', 'finished_ns')}


def audit(tasks, policy):
    """Return bounded witnesses plus full counts. Invalid evidence raises ValueError.

    FIFO legitimately selects normal tasks while urgent tasks wait; this is
    recorded as behavior, and only constitutes a violation for priority policy.
    """
    if policy not in ('fifo', 'priority'):
        raise ValueError('policy must be fifo or priority')
    rows = _read_tasks(tasks)
    result = {
        'policy': policy, 'completed_tasks': len(rows),
        'audited_dispatches': 0, 'first_dispatch_not_audited': True,
        'urgent_overtake_decisions': 0, 'urgent_overtake_pairs': 0,
        'normal_with_waiting_urgent_decisions': 0,
        'normal_with_waiting_urgent_pairs': 0,
        'priority_violation_decisions': 0, 'priority_violation_pairs': 0,
        'nonpreemptive_overlap_count': 0,
        'same_priority_fifo': {},
        'fifo_dispatch_order': [r['task_id'] for r in rows] == sorted(r['task_id'] for r in rows),
        'sample_limit': SAMPLE_LIMIT,
        'samples': {'urgent_overtakes': [], 'normal_with_waiting_urgent': [],
                    'overlaps': [], 'same_priority_fifo_inversions': []},
        'explanation': (
            'Integer CLOCK_MONOTONIC ns. started_ns is after dequeue. Only tasks '
            'enqueued no later than the previous task finished_ns are certain '
            'to have been waiting before the current dequeue. First dispatch is '
            'not audited. FIFO waiting-urgent cases are expected behavior. '
            'No observed violation does not prove behavior under unobserved contention.'),
    }
    for priority in ('normal', 'urgent'):
        group = [r for r in rows if r['priority'] == priority]
        result['same_priority_fifo'][priority] = all(a['task_id'] < b['task_id'] for a,b in zip(group,group[1:]))
        for a,b in zip(group,group[1:]):
            if a['task_id'] > b['task_id'] and len(result['samples']['same_priority_fifo_inversions']) < SAMPLE_LIMIT:
                result['samples']['same_priority_fifo_inversions'].append({'before': _identity(a), 'after': _identity(b)})
    for previous, current in zip(rows, rows[1:]):
        boundary = previous['finished_ns']
        if boundary > current['started_ns']:
            result['nonpreemptive_overlap_count'] += 1
            if len(result['samples']['overlaps']) < SAMPLE_LIMIT:
                result['samples']['overlaps'].append({'previous':_identity(previous),'current':_identity(current)})
            continue  # Invalid single-worker timeline cannot witness a dequeue.
        result['audited_dispatches'] += 1
        waiting = [r for r in rows if r['enqueued_ns'] <= boundary and r['started_ns'] > current['started_ns']]
        if current['priority'] == 'urgent':
            overtaken = [r for r in waiting if r['priority'] == 'normal' and r['task_id'] < current['task_id']]
            if overtaken:
                result['urgent_overtake_decisions'] += 1
                result['urgent_overtake_pairs'] += len(overtaken)
                for other in overtaken:
                    if len(result['samples']['urgent_overtakes']) < SAMPLE_LIMIT:
                        result['samples']['urgent_overtakes'].append({'selected':_identity(current), 'waiting':_identity(other), 'safe_boundary_ns':boundary})
        else:
            waiting_urgent = [r for r in waiting if r['priority'] == 'urgent']
            if waiting_urgent:
                result['normal_with_waiting_urgent_decisions'] += 1
                result['normal_with_waiting_urgent_pairs'] += len(waiting_urgent)
                for other in waiting_urgent:
                    if len(result['samples']['normal_with_waiting_urgent']) < SAMPLE_LIMIT:
                        result['samples']['normal_with_waiting_urgent'].append({'selected':_identity(current), 'waiting':_identity(other), 'safe_boundary_ns':boundary})
    if policy == 'priority':
        result['priority_violation_decisions'] = result['normal_with_waiting_urgent_decisions']
        result['priority_violation_pairs'] = result['normal_with_waiting_urgent_pairs']
    result['priority_behavior_observed'] = result['urgent_overtake_decisions'] > 0
    result['policy_ok'] = (result['nonpreemptive_overlap_count'] == 0 and
                           all(result['same_priority_fifo'].values()) and
                           (result['fifo_dispatch_order'] if policy == 'fifo' else result['priority_violation_pairs'] == 0))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks', type=Path, required=True)
    parser.add_argument('--policy', choices=('fifo','priority'), required=True)
    args = parser.parse_args()
    try:
        result = audit(args.tasks, args.policy)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['policy_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
