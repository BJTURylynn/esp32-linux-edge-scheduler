"""Hardware-only dispatcher acceptance. No emulated worker or compute fallback.

Only after stopping and archiving the owned service does the separate verifier
calculate independent reference values. Network failures never become results.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from .dispatch_client import DispatchClient, DispatchError
from .dispatch_gateway import run_gateway
from .dispatch_start import ROOT, supervisor_command, validate
from .dispatch_verify import verify
from .experiment import free_port
from .project_demo import read_events, wait_event


FIXED_CASES = (
    dict(seed=0, count=1, window=1, passes=1),
    dict(seed=100, count=16, window=8, passes=10),
    dict(seed=200, count=256, window=16, passes=10),
    dict(seed=4294967295, count=16, window=16, passes=1),
    dict(seed=0, count=256, window=8, passes=1000),
)


class HardwareCheck:
    def __init__(self, args, client, directory, cancelled, deadline=None):
        self.args, self.client, self.directory, self.cancelled = args, client, directory, cancelled
        self.deadline = deadline or time.monotonic() + args.timeout
        self.gateways = {}
        self.attempts = {'uno-01': 0, 'esp32c3-01': 0}
        self.phases = []
        self.errors = queue.Queue()
        self.run_id = client.state()['run_id']
        self.last_signature = None
        self.observations = (directory/'observations.jsonl').open('w', encoding='utf-8')

    def close(self):
        failures = []
        for device in list(self.gateways):
            try:
                self.stop_node(device)
            except (OSError, ValueError, RuntimeError) as exc:
                failures.append(str(exc))
        self.observations.close()
        if failures:
            raise RuntimeError('; '.join(failures))

    def state(self):
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            raise TimeoutError('hardware acceptance cancelled or overall deadline reached')
        if not self.errors.empty():
            raise RuntimeError(self.errors.get())
        state = self.client.state()
        if state['run_id'] != self.run_id:
            raise RuntimeError('central run changed during hardware acceptance')
        signature = (tuple((node['device'], node['state'], node['task_id']) for node in state['nodes']),
                     tuple((task['id'], task['state']) for task in state['tasks']))
        if signature != self.last_signature:
            self.observations.write(json.dumps(dict(monotonic_ns=time.monotonic_ns(),
                run_id=self.run_id, queue_depth=state['queue_depth'], counts=state['counts'],
                nodes=state['nodes'], tasks=[dict(id=t['id'], state=t['state'], device=t['device'])
                                           for t in state['tasks']]))+'\n')
            self.observations.flush()
            self.last_signature = signature
        return state

    def wait(self, predicate, label, timeout=35):
        deadline = min(self.deadline, time.monotonic()+timeout)
        while time.monotonic() < deadline:
            state = self.state()
            if predicate(state):
                return state
            self.cancelled.wait(.05)
        raise TimeoutError(label)

    @staticmethod
    def node(state, device):
        return next(node for node in state['nodes'] if node['device'] == device)

    @staticmethod
    def task(state, identity):
        return next(task for task in state['tasks'] if task['id'] == identity)

    def start_node(self, device):
        if device in self.gateways:
            raise RuntimeError('gateway already owned for '+device)
        self.attempts[device] += 1
        event = threading.Event()
        def runner():
            try:
                run_gateway(device, self.directory/f'gateway-{device}-{self.attempts[device]}',
                    central_port=self.client.port, stop=event,
                    serial_port=self.args.uno_port if device == 'uno-01' else None,
                    esp_host=self.args.esp_host if device == 'esp32c3-01' else None)
            except Exception as exc:
                self.errors.put(f'{device} gateway failed: {type(exc).__name__}')
        thread = threading.Thread(target=runner, daemon=True)
        self.gateways[device] = event, thread
        thread.start()
        self.wait(lambda state: self.node(state, device)['state'] == 'idle',
                  device+' did not complete physical handshake', 18)

    def stop_node(self, device):
        if device not in self.gateways:
            return
        event, thread = self.gateways.pop(device)
        event.set()
        thread.join(8)
        if thread.is_alive():
            raise RuntimeError('owned gateway would not stop: '+device)
        if not self.cancelled.is_set() and time.monotonic() < self.deadline:
            self.wait(lambda state: self.node(state, device)['state'] in ('offline', 'error'),
                      device+' still registered after gateway disconnect', 5)

    def submit(self, priority='normal', **parameters):
        receipt = self.client.submit(dict(priority=priority, run_id=self.run_id, **parameters))
        if not receipt['ok'] or len(receipt['accepted']) != 1 or receipt['rejected']:
            raise RuntimeError('task unexpectedly refused: '+json.dumps(receipt))
        return receipt['accepted'][0]['task_id']

    def completed(self, identity, device=None):
        state = self.wait(lambda current: self.task(current, identity)['state']
                          in ('succeeded', 'failed', 'unknown'), f'task {identity} did not finish')
        task = self.task(state, identity)
        if task['state'] != 'succeeded' or device and task['device'] != device:
            raise RuntimeError('real device task failed or wrong device: '+json.dumps(task))
        return task

    def record(self, name, **values):
        item = dict(phase=name, passed=True, **values)
        self.phases.append(item)
        print(json.dumps(item), flush=True)
        (self.directory/'phases.json').write_text(json.dumps(self.phases, indent=2)+'\n', encoding='utf-8')

    def execute(self):
        # No relay is started until each phase explicitly requests real hardware.
        for device in ('uno-01', 'esp32c3-01'):
            self.start_node(device)
            tasks = []
            for case in FIXED_CASES:
                task_id = self.submit(**case)
                tasks.append(self.completed(task_id, device))
            self.record('fixed-'+device, tasks=tasks)
            self.stop_node(device)

        # Uno gets the first heavy job while ESP is disconnected; only the
        # central scheduler chooses devices once ESP joins the backlogged run.
        self.start_node('uno-01')
        heavy = self.submit(seed=1, count=256, window=16, passes=1000)
        self.wait(lambda state: self.task(state, heavy)['state'] == 'running', 'Uno heavy task never accepted', 4)
        mixed = []
        for index in range(16):
            mixed.append(self.submit(priority='urgent' if index in (4, 9, 14) else 'normal',
                                     seed=300+index, count=16, window=8, passes=1))
        backlog = self.state()
        if backlog['queue_depth'] < 2:
            raise RuntimeError('heavy device did not create observable contention')
        self.start_node('esp32c3-01')
        for task_id in [heavy, *mixed]:
            self.completed(task_id)
        self.record('dual-backlog', heavy=heavy, queued_tasks=mixed, backlog=backlog['queue_depth'])

        self.stop_node('esp32c3-01')
        original = self.submit(seed=55, count=256, window=16, passes=1000)
        self.wait(lambda state: self.task(state, original)['state'] == 'running', 'failure task never accepted', 4)
        self.stop_node('uno-01')
        self.wait(lambda state: self.task(state, original)['state'] == 'unknown', 'disconnect not marked unknown', 5)
        self.start_node('uno-01')
        receipt = self.client.retry(original, run_id=self.run_id)
        if not receipt['ok']:
            raise RuntimeError('manual retry refused')
        replacement = receipt['task_id']
        task = self.completed(replacement, 'uno-01')
        after = self.state()
        if replacement == original or task['retry_of'] != original or self.task(after, original)['state'] != 'unknown':
            raise RuntimeError('manual retry changed or reused original task')
        self.record('disconnect-and-manual-retry', original=original, retry=replacement)
        self.stop_node('uno-01')

        waiting = self.submit(seed=777, count=1, window=1, passes=1)
        self.cancelled.wait(.25)
        before = self.state()
        if self.task(before, waiting)['state'] != 'queued' or any(node['state'] not in ('offline', 'error') for node in before['nodes']):
            raise RuntimeError('task completed or dispatched with both hardware links absent')
        # Fill the remaining central slots, prove bounded refusal, then drain
        # through the real boards. No generated result is supplied by this tool.
        queued_ids = [waiting]
        for index in range(63):
            queued_ids.append(self.submit(seed=1000+index, count=1, window=1, passes=1))
        rejected = self.client.submit(dict(run_id=self.run_id, seed=9999, count=1, window=1, passes=1))
        if rejected['accepted'] or [item['error'] for item in rejected['rejected']] != ['queue_full']:
            raise RuntimeError('full central queue did not reject the next task')
        full = self.state()
        if full['queue_depth'] != 64 or any(self.task(full, task_id)['state'] != 'queued' for task_id in queued_ids):
            raise RuntimeError('offline queue was processed without a real device')
        self.record('offline-and-queue-full', queued_ids=queued_ids, rejected=rejected)
        self.start_node('uno-01')
        self.start_node('esp32c3-01')
        for task_id in queued_ids:
            self.completed(task_id)
        final = self.state()
        if final['queue_depth'] or any(task['state'] not in ('succeeded', 'unknown') for task in final['tasks']):
            raise RuntimeError('unexpected final task state')
        self.record('real-hardware-drain', succeeded=final['counts']['succeeded'])
        return final


def run(args):
    # Reuse startup validation; web_port=0 is the explicit no-web harness mode.
    started = time.monotonic()
    selected = argparse.Namespace(**vars(args))
    selected.web_port = args.web_port or 8766
    validate(selected)
    if type(args.timeout) not in (int, float) or not 30 <= args.timeout <= 300:
        raise ValueError('hardware acceptance timeout must be 30..300 seconds')
    args.output = args.output.resolve()
    if args.output.exists():
        raise FileExistsError('choose a new output directory')
    port = free_port()
    web = web_thread = None
    if args.web_port:
        from .web_agent import WebApp, make_server
        web = make_server(WebApp(ROOT/'results/live/status.json', ROOT/'results/comparison/report.json',
                                 dispatch_port=port), port=args.web_port)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError:
        if web:
            web.server_close()
        raise
    cancelled = threading.Event()
    prior = {sig: signal.signal(sig, lambda *unused: cancelled.set())
             for sig in (signal.SIGINT, signal.SIGTERM)}
    result = dict(success=False, hardware_validated=False, error=None, central_port=port,
                  policy=args.policy, data_source='synthetic', phases=[], verification=None,
                  web_url=f'http://127.0.0.1:{args.web_port}/#dispatch' if web else None)
    process = reader = check = None
    events = queue.Queue(maxsize=8)
    try:
        with (args.output/'supervisor.log').open('w', encoding='utf-8') as log, \
             (args.output/'supervisor-stderr.log').open('wb') as errors:
            try:
                process = subprocess.Popen(supervisor_command(args, ROOT, port), cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                    text=True, encoding='utf-8', errors='replace', bufsize=1)
                reader = threading.Thread(target=read_events, args=(process.stdout, events, log), daemon=True)
                reader.start()
                ready = wait_event(process, events, 'ready', min(80, args.timeout))
                client = DispatchClient(port=port)
                deadline = time.monotonic()+8
                while True:
                    try:
                        if client.state()['run_id'] != ready['run_id']:
                            raise RuntimeError('wrong dispatcher at forwarded port')
                        break
                    except DispatchError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(.1)
                if web:
                    web_thread = threading.Thread(target=web.serve_forever, daemon=True)
                    web_thread.start()
                check = HardwareCheck(args, client, args.output, cancelled,
                                      deadline=started+args.timeout)
                result['run_id'] = check.run_id
                print(json.dumps(dict(event='ready', central_port=port, run_id=check.run_id,
                                      web_url=result['web_url'], hardware_only=True)), flush=True)
                result['last_live_state'] = check.execute()
                result['phases'] = check.phases
            except (OSError, ValueError, RuntimeError, DispatchError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
                result['error'] = type(exc).__name__+': '+str(exc)
            finally:
                if check:
                    result['phases'] = check.phases
                    try:
                        check.close()
                    except (OSError, ValueError, RuntimeError):
                        result['error'] = result['error'] or 'gateway cleanup failed'
                if web_thread:
                    web.shutdown(); web_thread.join(3)
                if web:
                    web.server_close()
                if process:
                    if process.poll() is None:
                        try:
                            process.stdin.write('STOP\n'); process.stdin.flush()
                        except (OSError, ValueError):
                            pass
                    try:
                        process.stdin.close(); process.wait(timeout=45)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill(); process.wait(timeout=5)
                        result['error'] = result['error'] or 'supervisor forced termination'
                    if process.returncode:
                        result['error'] = result['error'] or f'supervisor exit {process.returncode}'
                    if reader:
                        reader.join(2)
                    process.stdout.close()
        if result['error'] is None:
            # Deliberately separate post-run reference calculation from all
            # gateway/device execution. One unknown is intentional fault proof.
            result['verification'] = verify(args.output/'service', minimum=2*len(FIXED_CASES))
            audit = result['verification']['scheduling']
            if not audit['policy_verified'] or audit['max_inflight'] < 2:
                raise ValueError('hardware schedule or parallel in-flight evidence not verified')
            if args.policy == 'priority' and audit['urgent_overtake_decisions'] < 1:
                raise ValueError('no real priority contention witness observed')
            result['success'] = result['hardware_validated'] = True
    except (OSError, ValueError, RuntimeError) as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
    finally:
        for sig, handler in prior.items():
            signal.signal(sig, handler)
        result['elapsed_seconds'] = round(time.monotonic()-started, 3)
        (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        text = ['# 双设备计算实物验收', '', '通过' if result['success'] else '未通过',
                '任务只由真实 Uno / ESP32-C3 计算，参考计算只在停服归档之后执行。',
                '断线阶段故意产生一个 unknown；其后重试是显式新任务，不是自动重发。',
                '并发证据指中央在途区间重叠，不声称 MCU 时钟同步或 CPU 指令级同时执行。', '',
                '失败原因：'+str(result['error']) if result['error'] else '各阶段、原始转发、C日志和数值核对随目录保留。']
        (args.output/'report.md').write_text('\n'.join(text)+'\n', encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--esp-host', required=True)
    p.add_argument('--uno-port', required=True)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--distribution', default='Ubuntu-24.04')
    p.add_argument('--policy', choices=('priority', 'fifo'), default='priority')
    p.add_argument('--web-port', type=int, default=0, help='0 disables the optional observation page')
    p.add_argument('--timeout', type=float, default=300)
    args = p.parse_args()
    try:
        result = run(args)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(dict(success=result['success'], error=result['error'],
                          hardware_validated=result['hardware_validated'],
                          web_url=result['web_url'])), flush=True)
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
