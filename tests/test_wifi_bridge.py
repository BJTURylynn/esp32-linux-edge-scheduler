"""Actual TCP tests: fragmentation, truncation and malformed device sessions."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from tools.protocol import encode
from tools.wifi_bridge import run_bridge


def message(seq):
    return encode(dict(device_id='esp32c3-01', seq=seq, priority='normal',
                       source='synthetic', samples=[2,4,6,8]))


class WifiBridgeTests(unittest.TestCase):
    def session(self, chunks, count=2):
        sink = socket.socket()
        device = socket.socket()
        for server in (sink, device):
            server.bind(('127.0.0.1', 0))
            server.listen(1)
            server.settimeout(5)
            self.addCleanup(server.close)
        data = bytearray()
        command = bytearray()
        errors = []
        def receive():
            try:
                with sink.accept()[0] as conn:
                    conn.settimeout(5)
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk: break
                        data.extend(chunk)
            except Exception as e: errors.append(e)
        def produce():
            try:
                with device.accept()[0] as conn:
                    conn.settimeout(5)
                    while not command.endswith(b'\n'):
                        chunk = conn.recv(32)
                        if not chunk: break
                        command.extend(chunk)
                    for chunk in chunks:
                        conn.sendall(chunk)
            except (BrokenPipeError, ConnectionResetError): pass
            except Exception as e: errors.append(e)
        threads = [threading.Thread(target=receive, daemon=True),
                   threading.Thread(target=produce, daemon=True)]
        for thread in threads: thread.start()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'capture'
            result = run_bridge('127.0.0.1', output, device_port=device.getsockname()[1],
                                linux_port=sink.getsockname()[1], count=count, timeout=5)
            capture = (output / 'frames.jsonl').read_bytes()
            self.assertEqual(json.loads((output/'stats.json').read_text())['success'], result['success'])
        for thread in threads:
            thread.join(6)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(command, f'START {count}\n'.encode())
        return result, capture, bytes(data)

    def test_split_and_coalesced_frames_reach_real_tcp_sink(self):
        a, b = message(0), message(1)
        result, capture, received = self.session([a[:9], a[9:]+b])
        self.assertTrue(result['success'])
        self.assertEqual(result['forwarded'], 2)
        self.assertEqual(received, a+b)
        self.assertEqual(capture, a+b)

    def test_partial_eof_is_not_success(self):
        result, capture, received = self.session([message(0), message(1)[:-1]])
        self.assertFalse(result['success'])
        self.assertEqual(result['incomplete'], 1)
        self.assertEqual(received, capture)
        self.assertEqual(received, message(0))

    def test_oversized_line_is_rejected(self):
        result, capture, received = self.session([b'x'*9000+b'\n'])
        self.assertFalse(result['success'])
        self.assertEqual(result['oversized'], 1)
        self.assertEqual(received, b'')

    def test_invalid_json_never_reaches_linux(self):
        result, capture, received = self.session([b'{"bad":1}\n'])
        self.assertFalse(result['success'])
        self.assertEqual(result['invalid'], 1)
        self.assertEqual(received, b'')

    def test_early_eof_is_not_success(self):
        result, capture, received = self.session([message(0)])
        self.assertFalse(result['success'])
        self.assertEqual(result['forwarded'], 1)

    def test_excess_frames_fail_without_forwarding_extra(self):
        result, capture, received = self.session([message(0)+message(1)], count=1)
        self.assertFalse(result['success'])
        self.assertEqual(received, message(0))


if __name__ == '__main__':
    unittest.main()
