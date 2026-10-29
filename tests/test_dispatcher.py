"""Real C central dispatcher, simulated device results (no hardware or LLM)."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
_legacy = Path(os.environ.get('EDGE_SERVER', ROOT / 'linux/build/edge_scheduler'))
BINARY = Path(os.environ.get('EDGE_DISPATCHER', _legacy.with_name('edge_dispatcher')))


def encoded(value):
    return (json.dumps(value, separators=(',', ':'), allow_nan=False) + '\n').encode()


class Central:
    def __init__(self, folder, policy='priority', capacity=64, accept_timeout=3, task_timeout=30):
        self.output = Path(folder) / 'central'
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        self.log = (Path(folder) / 'server.log').open('w+')
        self.process = subprocess.Popen([str(BINARY), '--port', str(self.port), '--policy', policy,
            '--capacity', str(capacity), '--accept-timeout', str(accept_timeout),
            '--task-timeout', str(task_timeout), '--output', str(self.output)], stdout=self.log, stderr=self.log)
        self.workers = []
        self.wait(lambda value: value.get('running'))

    def request(self, value):
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as connection:
            connection.sendall(encoded(value))
            with connection.makefile('rb') as reader:
                return json.loads(reader.readline(262145))

    def state(self):
        return self.request({'v': 2, 'type': 'state'})

    def submit(self, **kwargs):
        return self.request({'v': 2, 'type': 'submit', 'priority': 'normal', 'seed': 0,
                             'count': 256, 'window': 8, 'passes': 100, **kwargs})

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                raise AssertionError('central exited: ' + self.log.read())
            try:
                state = self.state()
            except OSError:
                time.sleep(.01)
                continue
            if predicate(state):
                return state
            time.sleep(.01)
        raise AssertionError('central condition timed out: ' + repr(self.state()))

    def worker(self, device='uno-01'):
        result = Worker(self, device)
        self.workers.append(result)
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for worker in self.workers:
            worker.close()
        self.log.seek(0)
        log = self.log.read()
        self.log.close()
        if self.process.returncode:
            raise AssertionError('central stopped nonzero: ' + log)


class Worker:
    def __init__(self, central, device):
        self.device = device
        self.socket = socket.create_connection(('127.0.0.1', central.port), timeout=3)
        self.reader = self.socket.makefile('rb')
        self.send({'v': 2, 'type': 'hello', 'device': device})
        welcome = self.read()
        if welcome.get('type') != 'welcome':
            self.close()
            raise AssertionError(welcome)
        self.session = welcome['session']

    def send(self, value):
        self.socket.sendall(encoded(value))

    def read(self):
        line = self.reader.readline(258)
        if not line:
            raise EOFError('central closed worker')
        return json.loads(line)

    def task(self):
        while True:
            packet = self.read()
            if packet['type'] == 'ping':
                self.send({'v': 2, 'type': 'pong', 'device': self.device, 'session': self.session})
            else:
                if packet['type'] != 'task':
                    raise AssertionError(packet)
                return packet

    def response(self, task, kind, **kwargs):
        return {'v': 2, 'type': kind, 'device': self.device, 'session': self.session,
                'id': task['id'], **kwargs}

    def accept(self, task):
        self.send(self.response(task, 'accept'))

    def finish(self, task, **kwargs):
        self.send(self.response(task, 'result', mean=0.25, rms=1.5, compute_us=125, **kwargs))

    def close(self):
        if getattr(self, 'reader', None):
            self.reader.close()
            self.reader = None
        if getattr(self, 'socket', None):
            self.socket.close()
            self.socket = None


@unittest.skipUnless(os.name == 'posix' and BINARY.is_file(), 'requires compiled Linux edge_dispatcher')
class DispatcherTests(unittest.TestCase):
    def start(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        central = Central(temporary.name, **kwargs)
        self.addCleanup(central.close)
        return central

    def test_no_worker_retains_bounded_queue_and_never_calculates(self):
        central = self.start(capacity=2)
        self.assertTrue(central.submit()['ok'])
        self.assertTrue(central.submit(priority='urgent')['ok'])
        self.assertEqual(central.submit()['error'], 'queue_full')
        state = central.state()
        self.assertEqual(state['queue_depth'], 2)
        self.assertEqual(state['counts']['succeeded'], 0)
        self.assertEqual(state['counts']['rejected'], 1)
        self.assertTrue(all(t['mean'] is None and t['device'] is None for t in state['tasks']))
        self.assertTrue(all(n['state'] == 'offline' for n in state['nodes']))

    def test_priority_selects_urgent_then_oldest_normal(self):
        central = self.start()
        ids = [central.submit(seed=11)['task_id'], central.submit(seed=12)['task_id'],
               central.submit(priority='urgent', seed=13)['task_id']]
        worker = central.worker()
        for expected in (ids[2], ids[0], ids[1]):
            task = worker.task()
            self.assertEqual(task['id'], expected)
            worker.accept(task)
            worker.finish(task)
        state = central.wait(lambda s: s['counts']['succeeded'] == 3)
        self.assertEqual(state['queue_depth'], 0)
        self.assertEqual(state['tasks'][0]['device'], 'uno-01')

    def test_fifo_preserves_arrival_order_regardless_of_priority(self):
        central = self.start(policy='fifo')
        first = central.submit()['task_id']
        second = central.submit(priority='urgent')['task_id']
        worker = central.worker()
        for expected in (first, second):
            task = worker.task()
            self.assertEqual(task['id'], expected)
            worker.accept(task)
            worker.finish(task)
        central.wait(lambda s: s['counts']['succeeded'] == 2)

    def test_two_nodes_have_only_one_inflight_each_and_actual_result_required(self):
        central = self.start()
        uno, esp = central.worker(), central.worker('esp32c3-01')
        for _ in range(4):
            central.submit()
        a, b = uno.task(), esp.task()
        self.assertNotEqual(a['id'], b['id'])
        uno.accept(a)
        esp.accept(b)
        state = central.wait(lambda s: s['counts']['running'] == 2)
        self.assertEqual(state['queue_depth'], 2)
        self.assertEqual(state['counts']['succeeded'], 0)
        uno.finish(a)
        c = uno.task()
        self.assertNotIn(c['id'], (a['id'], b['id']))
        uno.accept(c)
        uno.finish(c)
        d = uno.task()
        uno.accept(d)
        uno.finish(d)
        esp.finish(b)
        state = central.wait(lambda s: s['counts']['succeeded'] == 4)
        self.assertEqual(state['tasks'][0]['compute_us'], 125)
        self.assertGreaterEqual(state['tasks'][0]['roundtrip_ms'], 0)

    def test_wrong_identity_old_session_and_unaccepted_result_never_complete(self):
        central = self.start()
        worker = central.worker()
        central.submit()
        task = worker.task()
        worker.finish(task)  # Must first acknowledge accepting this task.
        worker.send(worker.response(task, 'result', mean=1, rms=2, compute_us=2, session='0' * 16))
        worker.send(worker.response(task, 'result', mean=1, rms=2, compute_us=2, device='esp32c3-01'))
        worker.send(worker.response(task, 'accept', id=task['id'] + 100))
        self.assertEqual(central.state()['counts']['succeeded'], 0)
        worker.accept(task)
        worker.finish(task)
        central.wait(lambda s: s['counts']['succeeded'] == 1)
        worker.finish(task)  # Duplicate cannot increment completed amount.
        self.assertEqual(central.state()['counts']['succeeded'], 1)

    def test_disconnect_is_unknown_and_retry_is_new_linked_task(self):
        central = self.start()
        worker = central.worker()
        original = central.submit(seed=65)['task_id']
        task = worker.task()
        worker.accept(task)
        worker.close()
        state = central.wait(lambda s: s['counts']['unknown'] == 1)
        self.assertEqual(state['tasks'][0]['state'], 'unknown')
        self.assertEqual(state['counts']['succeeded'], 0)
        new = central.request({'v': 2, 'type': 'retry', 'id': original})['task_id']
        self.assertNotEqual(original, new)
        replacement = central.worker()
        self.assertNotEqual(replacement.session, worker.session)
        task = replacement.task()
        self.assertEqual(task['id'], new)
        self.assertEqual(task['seed'], 65)
        replacement.accept(task)
        replacement.finish(task)
        state = central.wait(lambda s: s['counts']['succeeded'] == 1)
        self.assertEqual(state['tasks'][1]['retry_of'], original)
        self.assertEqual(state['tasks'][0]['state'], 'unknown')
        self.assertEqual(central.request({'v': 2, 'type': 'retry', 'id': new})['error'], 'not_retryable')

    def test_ack_and_result_deadlines_close_node_without_fake_success(self):
        for accept in (False, True):
            with self.subTest(accept=accept):
                central = self.start(accept_timeout=.1, task_timeout=.25)
                worker = central.worker()
                central.submit()
                task = worker.task()
                if accept:
                    worker.accept(task)
                state = central.wait(lambda s: s['counts']['unknown'] == 1)
                self.assertEqual(state['tasks'][0]['error'], 'task_timeout' if accept else 'accept_timeout')
                self.assertEqual(state['counts']['succeeded'], 0)
                self.assertEqual(state['nodes'][0]['state'], 'error')

    def test_worker_error_is_failed_and_can_be_manually_retried(self):
        central = self.start()
        worker = central.worker()
        original = central.submit()['task_id']
        task = worker.task()
        worker.send(worker.response(task, 'error', reason='compute_error'))
        state = central.wait(lambda s: s['counts']['failed'] == 1)
        self.assertEqual(state['tasks'][0]['error'], 'compute_error')
        self.assertTrue(central.request({'v': 2, 'type': 'retry', 'id': original})['ok'])

    def test_firmware_error_enums_never_look_like_successful_results(self):
        for reason in ('duplicate_task', 'nonfinite_result'):
            with self.subTest(reason=reason):
                central = self.start()
                worker = central.worker()
                central.submit()
                task = worker.task()
                if reason == 'nonfinite_result':
                    worker.accept(task)
                worker.send(worker.response(task, 'error', reason=reason))
                state = central.wait(lambda s: s['counts']['failed'] == 1)
                self.assertEqual(state['tasks'][0]['error'], reason)
                self.assertIsNone(state['tasks'][0]['mean'])
                self.assertEqual(state['counts']['succeeded'], 0)
                self.assertTrue(central.request({'v': 2, 'type': 'retry',
                                                'id': task['id']})['ok'])

    def test_run_id_guards_reused_task_numbers_and_batch_submission(self):
        previous = self.start()
        stale_run = previous.state()['run_id']
        old_id = previous.submit()['task_id']
        central = self.start()
        run = central.state()['run_id']
        self.assertNotEqual(run, stale_run)
        self.assertEqual(central.submit(run_id=stale_run)['error'], 'stale_run')
        self.assertEqual(central.state()['tasks'], [])
        new_id = central.submit(run_id=run)['task_id']
        self.assertEqual(old_id, new_id)  # Same number now names another run.
        worker = central.worker()
        task = worker.task()
        worker.send(worker.response(task, 'error', reason='compute_error'))
        central.wait(lambda s: s['counts']['failed'] == 1)
        self.assertEqual(central.request({'v': 2, 'type': 'retry', 'id': old_id,
                                         'run_id': stale_run})['error'], 'stale_run')
        self.assertEqual(len(central.state()['tasks']), 1)
        response = central.request({'v': 2, 'type': 'retry', 'id': new_id, 'run_id': run})
        self.assertTrue(response['ok'])
        self.assertEqual(central.state()['tasks'][1]['retry_of'], new_id)

    def test_malformed_requests_and_parameter_bounds_are_rejected(self):
        central = self.start()
        for changes in ({'count': 0}, {'count': 257}, {'window': 17}, {'passes': 1001},
                        {'seed': -1}, {'seed': 4294967296}, {'count': True},
                        {'count': 1.0}, {'priority': 'arbitrary'}, {'path': '/etc/passwd'}):
            with self.subTest(changes=changes):
                self.assertEqual(central.submit(**changes)['error'], 'invalid_request')
        self.assertTrue(central.submit(seed=4294967295, count=1, passes=1000, window=16)['ok'])
        self.assertEqual(len(central.state()['tasks']), 1)
        with socket.create_connection(('127.0.0.1', central.port), timeout=3) as connection:
            connection.sendall(b'{"v":2,"v":2,"type":"state"}\n')
            self.assertIn(b'invalid_request', connection.recv(1024))

    def test_split_and_joined_worker_frames_are_handled(self):
        central = self.start()
        worker = central.worker()
        central.submit()
        task = worker.task()
        first = encoded(worker.response(task, 'accept'))
        second = encoded(worker.response(task, 'result', mean=1, rms=2, compute_us=3))
        worker.socket.sendall(first[:7])
        worker.socket.sendall(first[7:] + second)
        self.assertEqual(central.wait(lambda s: s['counts']['succeeded'] == 1)['tasks'][0]['rms'], 2)

    def test_duplicate_device_registration_and_oversized_frames_do_not_replace_node(self):
        central = self.start()
        worker = central.worker()
        with socket.create_connection(('127.0.0.1', central.port), timeout=3) as connection:
            connection.sendall(encoded({'v': 2, 'type': 'hello', 'device': 'uno-01'}))
            self.assertIn(b'duplicate_device', connection.recv(1024))
        self.assertEqual(central.state()['nodes'][0]['state'], 'idle')
        central.submit()
        worker.task()
        worker.socket.sendall(b'x' * 258 + b'\n')
        state = central.wait(lambda s: s['counts']['unknown'] == 1)
        self.assertEqual(state['tasks'][0]['error'], 'oversized_frame')


if __name__ == '__main__':
    unittest.main()
