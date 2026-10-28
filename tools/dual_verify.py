"""Cross-check both real device captures against one completed C service batch."""
import argparse
from collections import Counter
import csv,json,math
from pathlib import Path
from .protocol import reference,validate_message


def verify(capture,service):
    summary=json.loads((capture/'summary.json').read_text())
    if not summary['success']:raise ValueError('dual capture failed')
    requested=summary.get('requested_each')
    if type(requested) is not int or not 1<=requested<=100:
        raise ValueError('requested_each must be 1..100, not an empty batch')
    messages=[]
    for source,identity in [('arduino','uno-01'),('esp32','esp32c3-01')]:
        group=[validate_message(json.loads(line)) for line in (capture/source/'frames.jsonl').read_text().splitlines()]
        if len(group)!=summary['requested_each'] or any(m['device_id']!=identity for m in group):
            raise ValueError('source identity/count mismatch')
        messages.extend(group)
    state=json.loads((service/'status.json').read_text())
    with (service/'tasks.csv').open(newline='',encoding='utf-8') as f:rows=list(csv.DictReader(f))
    if state.get('fatal') is not False:
        raise ValueError('service has fatal or unknown terminal state')
    if (len(rows)!=len(messages) or state['completed']!=len(messages) or
        state['accepted']!=len(messages) or state['rejected']):
        raise ValueError('Linux counts do not match both captures')
    by_key={(r['device_id'],int(r['seq'])):r for r in rows}
    if len(by_key)!=len(rows):raise ValueError('duplicate Linux device/sequence')
    seen=set();maximum=0.0
    for message in messages:
        key=(message['device_id'],message['seq'])
        if key in seen:raise ValueError('duplicate captured device/sequence')
        seen.add(key)
        seq=message['seq'];urgent=seq%5==4;count=16 if urgent else 256
        expected_samples=[((i+seq%201)%201)-100 for i in range(count)]
        if message['samples']!=expected_samples or message['priority']!=('urgent' if urgent else 'normal'):
            raise ValueError('samples/priority differ from board generator')
        row=by_key[key]
        if row['status']!='completed' or row['source']!='synthetic' or row['priority']!=message['priority'] or int(row['samples'])!=count:
            raise ValueError('Linux metadata mismatch')
        expected=reference(message['samples'],state['window'])
        for metric in ('mean','rms'):
            error=abs(float(row[metric])-expected[metric])
            if not math.isfinite(error) or error>1e-8:raise ValueError('numerical mismatch')
            maximum=max(maximum,error)
    return dict(verified=True,completed=state['completed'],rejected=state['rejected'],
                devices=dict(Counter(m['device_id'] for m in messages)),
                priority=dict(Counter(m['priority'] for m in messages)),
                max_absolute_numeric_error=maximum,window=state['window'],passes=state['passes'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture',type=Path,required=True)
    p.add_argument('--service',type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(verify(a.capture,a.service),indent=2))


if __name__=='__main__':main()
