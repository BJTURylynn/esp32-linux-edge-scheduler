import json
import socket
import threading
import unittest

from tools.dispatch_gateway import Endpoint, packet, relay_session


def line(channel):
    pending = b''
    while b'\n' not in pending:
        part = channel.recv(1)
        if not part:
            raise EOFError('closed before line')
        pending += part
    return json.loads(pending)


def send(channel, value):
    channel.sendall(json.dumps(value, separators=(',', ':')).encode() + b'\n')


class DispatchGatewayTests(unittest.TestCase):
    def session(self):
        device, device_peer = socket.socketpair()
        central, central_peer = socket.socketpair()
        for channel in (device, central):
            channel.settimeout(.1)
        for channel in (device_peer, central_peer):
            channel.settimeout(2)
            self.addCleanup(channel.close)
        stopped = threading.Event()
        captured, errors = [], []
        def run():
            try:
                relay_session(Endpoint(device), Endpoint(central), 'uno-01', stopped,
                              lambda direction, value: captured.append((direction, value)))
            except (OSError, ValueError, TimeoutError):
                errors.append(True)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        def cleanup():
            stopped.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.addCleanup(cleanup)
        return device_peer, central_peer, stopped, thread, captured, errors

    def test_bidirectional_split_frames_and_no_computation(self):
        device, central, stop, thread, captured, _ = self.session()
        hello = b'{"v":2,"type":"hello","device":"uno-01"}\n'
        device.sendall(hello[:7]); device.sendall(hello[7:])
        self.assertEqual(line(central)['type'], 'hello')
        welcome = {'v': 2, 'type': 'welcome', 'session': '0123456789abcdef'}
        task = dict(v=2, type='task', session=welcome['session'], id=5,
                    seed=0, count=16, window=8, passes=100)
        central.sendall((json.dumps(welcome)+'\n'+json.dumps(task)+'\n').encode())
        self.assertEqual(line(device), welcome)
        self.assertEqual(line(device), task)
        accept = dict(v=2, type='accept', device='uno-01', session=welcome['session'], id=5)
        result = dict(v=2, type='result', device='uno-01', session=welcome['session'], id=5,
                      mean=-2.25, rms=3.5, compute_us=123)
        send(device, accept); send(device, result)
        self.assertEqual(line(central), accept)
        self.assertEqual(line(central), result)
        stop.set(); thread.join(3)
        self.assertEqual([value for direction, value in captured if value['type'] == 'result'], [result])

    def test_diagnostics_wrong_identity_and_nonfinite_packets_rejected(self):
        for raw in (b'WIFI_READY password=private',
                    b'{"v":2,"type":"hello","device":"esp32c3-01"}',
                    b'{"v":2,"type":"result","device":"uno-01","rms":1e999}',
                    b'{"v":2,"v":2,"type":"hello","device":"uno-01"}',
                    b'{"v":2,"type":"hello","device":"uno-01","x":[]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                packet(raw, 'device', 'uno-01')

    def test_oversized_frame_closes_both_links_without_forwarding(self):
        device, central, stop, thread, captured, errors = self.session()
        device.sendall(b'x' * 300 + b'\n')
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(captured, [])
        self.assertEqual(central.recv(1), b'')

    def test_disconnect_does_not_invent_result_or_replay_task(self):
        device, central, stop, thread, captured, errors = self.session()
        send(device, dict(v=2, type='hello', device='uno-01'))
        line(central)
        send(central, dict(v=2, type='welcome', session='0123456789abcdef'))
        line(device)
        task = dict(v=2, type='task', session='0123456789abcdef', id=9,
                    seed=0, count=1, window=1, passes=1)
        send(central, task)
        self.assertEqual(line(device), task)
        device.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(sum(message['type'] == 'task' for _, message in captured), 1)
        self.assertFalse(any(message['type'] == 'result' for _, message in captured))

    def test_duplicate_startup_hello_is_not_reregistered(self):
        device, central, stop, thread, captured, _ = self.session()
        hello = dict(v=2, type='hello', device='uno-01')
        send(device, hello)
        line(central)
        send(device, hello)
        send(central, dict(v=2, type='welcome', session='0123456789abcdef'))
        line(device)
        send(central, dict(v=2, type='ping', session='0123456789abcdef'))
        line(device)
        stop.set(); thread.join(3)
        self.assertEqual(sum(message['type'] == 'hello' for _, message in captured), 1)


if __name__ == '__main__':
    unittest.main()
