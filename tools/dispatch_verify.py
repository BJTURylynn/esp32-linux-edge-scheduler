"""Offline numerical verification of device results; never used by a gateway."""
import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path

from .dispatch_client import integer
from .protocol import reference


def scheduling_audit(rows, policy='priority'):
    if policy not in ('priority', 'fifo'):
        raise ValueError('unknown scheduling policy')
    parsed = [{**row, 'id': int(row['id']), 'enqueued_ns': int(row['enqueued_ns']),
               'dispatched_ns': int(row['dispatched_ns']), 'finished_ns': int(row['finished_ns'])}
              for row in rows]
    violations = []
    overtakes = 0
    dispatched = sorted((row for row in parsed if row['dispatched_ns']),
                        key=lambda row: (row['dispatched_ns'], row['id']))
    points = []
    for row in dispatched:
        moment = row['dispatched_ns']
        if not row['enqueued_ns'] <= moment <= row['finished_ns']:
            violations.append(dict(id=row['id'], reason='timestamp_order'))
        pending = [other for other in parsed if other['enqueued_ns'] <= moment
                   and (not other['dispatched_ns'] or other['dispatched_ns'] >= moment)
                   and (not other['finished_ns'] or other['finished_ns'] >= moment)]
        key = lambda candidate: ((0 if candidate['priority'] == 'urgent' else 1)
                                if policy == 'priority' else 0,
                                candidate['enqueued_ns'], candidate['id'])
        if not pending or min(pending, key=key)['id'] != row['id']:
            violations.append(dict(id=row['id'], reason='queue_selection'))
        if row['priority'] == 'urgent' and any(other['priority'] == 'normal'
                and other['enqueued_ns'] < row['enqueued_ns'] for other in pending):
            overtakes += 1
        points.extend([(moment, 1), (row['finished_ns'], -1)])
    active = maximum = 0
    for _, delta in sorted(points, key=lambda point: (point[0], point[1])):
        active += delta
        maximum = max(maximum, active)
    per_device = {}
    for device in {row.get('device') for row in dispatched} - {None, ''}:
        device_points = []
        for row in dispatched:
            if row.get('device') == device:
                device_points.extend([(row['dispatched_ns'], 1), (row['finished_ns'], -1)])
        current = peak = 0
        for _, delta in sorted(device_points, key=lambda point: (point[0], point[1])):
            current += delta
            peak = max(peak, current)
        per_device[device] = peak
        if peak > 1:
            violations.append(dict(device=device, reason='node_inflight_limit'))
    if maximum > 2:
        violations.append(dict(reason='central_inflight_limit'))
    return dict(policy=policy, policy_verified=not violations, violations=violations,
                dispatched=len(dispatched), max_inflight=maximum,
                per_device_max_inflight=per_device,
                urgent_overtake_decisions=overtakes,
                semantics='central dispatch-to-terminal intervals; no cross-device clock subtraction')


def verify(service, *, require_devices=('uno-01', 'esp32c3-01'), minimum=2):
    integer(minimum, 1, 512, 'minimum')
    with (Path(service)/'tasks.csv').open(newline='', encoding='utf-8') as source:
        rows = list(csv.DictReader(source))
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('duplicate terminal task IDs')
    good = [row for row in rows if row['state'] == 'succeeded']
    devices = Counter(row['device'] for row in good)
    if len(good) < minimum or set(require_devices) - set(devices):
        raise ValueError('not enough completed real-device records for requested verification')
    maximum = 0.0
    for row in good:
        seed = integer(int(row['seed']), 0, 4294967295, 'seed')
        count = integer(int(row['count']), 1, 256, 'count')
        window = integer(int(row['window']), 1, 16, 'window')
        integer(int(row['passes']), 1, 1000, 'passes')
        samples = [((index + seed % 201) % 201) - 100 for index in range(count)]
        expected = reference(samples, window)
        for metric in ('mean', 'rms'):
            actual = float(row[metric])
            delta = abs(actual - expected[metric])
            if not math.isfinite(actual) or delta > 1e-4 + 1e-4 * abs(expected[metric]):
                raise ValueError(f'task {row["id"]}: device {metric} differs from independent reference')
            maximum = max(maximum, delta)
        if not 0 <= int(row['compute_us']) <= 4294967295:
            raise ValueError('device compute duration is outside its uint32 range')
        if not 0 < int(row['dispatched_ns']) <= int(row['accepted_ns']) <= int(row['finished_ns']):
            raise ValueError('central task timestamps out of order')
    overlap = any(left['device'] != right['device']
                  and max(int(left['accepted_ns']), int(right['accepted_ns']))
                  < min(int(left['finished_ns']), int(right['finished_ns']))
                  for index, left in enumerate(good) for right in good[index+1:])
    state_path = Path(service)/'status.json'
    policy = json.loads(state_path.read_text())['policy'] if state_path.exists() else 'priority'
    audit = scheduling_audit(rows, policy)
    return dict(numerical_verified=True, succeeded=len(good), terminal_records=len(rows),
                all_tasks_succeeded=len(good) == len(rows), devices=dict(devices),
                states=dict(Counter(row['state'] for row in rows)),
                max_absolute_error=maximum, tolerance='1e-4 + 1e-4 * abs(reference)',
                zero_duration_records=sum(int(row['compute_us']) == 0 for row in good),
                duration_semantics='device micros resolution may round a tiny task to zero; duration alone does not prove execution',
                inflight_overlap_observed=overlap,
                scheduling=audit, policy_verified=audit['policy_verified'], max_inflight=audit['max_inflight'],
                overlap_semantics='central accept-to-result intervals overlap, not synchronized CPU traces',
                data_source='synthetic', reference_role='offline validation only, not task execution')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True, type=Path)
    parser.add_argument('--require-device', action='append', choices=('uno-01', 'esp32c3-01'))
    parser.add_argument('--minimum', type=int, default=2)
    args = parser.parse_args()
    try:
        result = verify(args.service, require_devices=args.require_device or ('uno-01', 'esp32c3-01'),
                        minimum=args.minimum)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
