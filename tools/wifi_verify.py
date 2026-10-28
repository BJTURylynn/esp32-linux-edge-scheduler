"""Verify a completed Wi-Fi batch against Linux CSV and numerical reference."""
import argparse
import csv
import json
import math
from pathlib import Path
from .protocol import reference, validate_message


def verify(capture, service):
    frames = [validate_message(json.loads(line)) for line in (capture/'frames.jsonl').read_text().splitlines()]
    stats = json.loads((capture/'stats.json').read_text())
    status = json.loads((service/'status.json').read_text())
    with (service/'tasks.csv').open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if not frames or not stats['success'] or stats['forwarded'] != len(frames):
        raise ValueError('missing frames or failed bridge session')
    if len(rows) != len(frames) or status['accepted'] != len(frames) or status['completed'] != len(frames) or status['rejected']:
        raise ValueError('service totals do not exactly match the capture')
    by_key = {(row['device_id'],int(row['seq'])):row for row in rows}
    if len(by_key) != len(rows):
        raise ValueError('duplicate service sequence')
    seen = set()
    maximum = 0.0
    for message in frames:
        seq = message['seq']
        key = (message['device_id'],seq)
        if key in seen or message['device_id'] != 'esp32c3-01':
            raise ValueError('unexpected device or duplicate captured sequence')
        seen.add(key)
        urgent = seq % 5 == 4
        count = 16 if urgent else 256
        samples = [((i + seq % 201) % 201)-100 for i in range(count)]
        if message['samples'] != samples or message['priority'] != ('urgent' if urgent else 'normal'):
            raise ValueError('device samples or priority differ from documented synthetic generator')
        row = by_key[key]
        if row['status'] != 'completed' or row['source'] != 'synthetic' or row['priority'] != message['priority'] or int(row['samples']) != count:
            raise ValueError('task metadata differs')
        expected = reference(samples, window=status['window'])
        for metric in ('mean','rms'):
            actual = float(row[metric])
            if not math.isfinite(actual) or abs(actual-expected[metric]) > 1e-8:
                raise ValueError('numerical result differs')
            maximum = max(maximum,abs(actual-expected[metric]))
    return dict(verified=True, frames=len(frames), device_id='esp32c3-01',
                sequences=sorted(k[1] for k in seen),
                normal=sum(m['priority']=='normal' for m in frames),
                urgent=sum(m['priority']=='urgent' for m in frames),
                accepted=status['accepted'],completed=status['completed'],rejected=status['rejected'],
                max_absolute_numeric_error=maximum,window=status['window'],
                caveat='CSV timing starts at Linux full-frame reception, not device transmission')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture',type=Path,required=True)
    parser.add_argument('--service',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.capture,args.service),indent=2))


if __name__=='__main__':
    main()
