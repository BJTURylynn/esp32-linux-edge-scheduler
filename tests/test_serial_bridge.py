import contextlib
import io
import json
import types
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from tools.protocol import encode, synthetic
from tools.serial_bridge import BridgeConfig, LineFramer, main, run_bridge, validated_frame


class Clock:
    def __init__(self):
        self.value = 0.0
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class FakeSerial:
    def __init__(self, chunks, clock):
        self.chunks = iter(chunks)
        self.clock = clock
        self.closed = False
        self.read_sizes = []
    def read(self, count):
        self.read_sizes.append(count)
        self.clock.value += 0.1
        value = next(self.chunks, b'')
        if isinstance(value, Exception):
            raise value
        return value
    def close(self):
        self.closed = True


class FakeSocket:
    def __init__(self, fail=False):
        self.fail = fail
        self.attempts = []
        self.closed = False
        self.shutdown_called = False
    def settimeout(self, timeout):
        self.timeout = timeout
    def sendall(self, frame):
        self.attempts.append(frame)
        if self.fail:
            raise OSError('simulated uncertain send')
    def shutdown(self, how):
        self.shutdown_called = True
    def close(self):
        self.closed = True


class SerialBridgeTests(unittest.TestCase):
    def test_fragmented_coalesced_and_crlf(self):
        framer = LineFramer(20)
        self.assertEqual(list(framer.feed(b'fir')), [])
        self.assertEqual(list(framer.feed(b'st\r\nsecond\nthird')), [b'first', b'second'])
        self.assertEqual(list(framer.feed(b'\n')), [b'third'])
        self.assertEqual(framer.finish(), 0)

    def test_oversized_line_is_bounded_and_next_line_survives(self):
        framer = LineFramer(8)
        for _ in range(1000):
            self.assertEqual(list(framer.feed(b'x' * 100)), [])
            self.assertLessEqual(len(framer.pending), 8)
        self.assertEqual(framer.oversized, 1)
        self.assertEqual(list(framer.feed(b'\nok\n')), [b'ok'])
        self.assertEqual(framer.finish(), 0)

    def test_exact_limit_and_partial_disconnect(self):
        framer = LineFramer(4)
        self.assertEqual(list(framer.feed(b'abcd\n')), [b'abcd'])
        self.assertEqual(list(framer.feed(b'abcd\r\n')), [b'abcd'])
        list(framer.feed(b'ab'))
        self.assertEqual(framer.finish(), 1)
        self.assertEqual(framer.finish(), 0)
        self.assertEqual(list(framer.feed(b'cd\n')), [b'cd'])

    def test_strict_validation_and_canonical_encoding(self):
        frame = encode(synthetic(4, 3, device_id='uno-01'))
        self.assertEqual(validated_frame(frame), frame)
        valid = frame.decode().strip()
        invalid = [valid.replace('"seq":4', '"seq":4,"seq":5'),
                   valid[:-1] + ',"extra":1}',
                   valid.replace('"synthetic"', '"sensor"'),
                   valid.replace('"seq":4', '"seq":4.0'),
                   valid.replace('"uno-01"', '"设备"')]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises((ValueError, UnicodeError)):
                validated_frame(item.encode('utf-8'))

    def test_success_capture_limit_and_clean_close(self):
        clock = Clock()
        frames = [encode(synthetic(i, 2)) for i in range(3)]
        serial = FakeSerial([b''.join(frames)], clock)
        tcp = FakeSocket()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'capture'
            result = run_bridge(BridgeConfig('COM10', output, limit=2),
                                lambda *a, **k: serial, lambda *a, **k: tcp, clock, clock.sleep)
            self.assertEqual(result['forwarded'], 2)
            self.assertEqual((output / 'frames.jsonl').read_bytes(), b''.join(frames[:2]))
            self.assertFalse(json.loads((output / 'stats.json').read_text())['running'])
            self.assertTrue(tcp.shutdown_called)
        self.assertTrue(serial.closed)
        self.assertTrue(tcp.closed)

    def test_uncertain_send_is_not_replayed(self):
        clock = Clock()
        frames = [encode(synthetic(i, 2)) for i in range(2)]
        serial = FakeSerial(frames, clock)
        bad, good = FakeSocket(fail=True), FakeSocket()
        sockets = iter([bad, good])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'capture'
            result = run_bridge(BridgeConfig('COM10', output, limit=1),
                                lambda *a, **k: serial, lambda *a, **k: next(sockets), clock, clock.sleep)
            self.assertEqual(result['uncertain'], 1)
            self.assertEqual(result['dropped'], 1)
            self.assertEqual(bad.attempts, [frames[0]])
            self.assertEqual(good.attempts, [frames[1]])
            self.assertEqual((output / 'frames.jsonl').read_bytes(), frames[1])

    def test_connect_failure_drops_without_claiming_uncertain_send(self):
        clock = Clock()
        frames = [encode(synthetic(i, 2)) for i in range(2)]
        serial = FakeSerial(frames, clock)
        tcp = FakeSocket()
        attempts = []
        def connect(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError('offline')
            return tcp
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bridge(BridgeConfig('COM10', Path(temporary) / 'capture', limit=1),
                                lambda *a, **k: serial, connect, clock, clock.sleep)
        self.assertEqual(result['tcp_errors'], 1)
        self.assertEqual(result['dropped'], 1)
        self.assertEqual(result['uncertain'], 0)
        self.assertEqual(tcp.attempts, [frames[1]])

    def test_serial_reconnect_discards_partial_frame(self):
        clock = Clock()
        frame = encode(synthetic(0, 2))
        first = FakeSerial([frame[:20], OSError('disconnected')], clock)
        second = FakeSerial([frame], clock)
        serials = iter([first, second])
        tcp = FakeSocket()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bridge(BridgeConfig('COM10', Path(temporary) / 'capture', limit=1),
                                lambda *a, **k: next(serials), lambda *a, **k: tcp, clock, clock.sleep)
        self.assertEqual(result['incomplete'], 1)
        self.assertEqual(result['serial_reconnects'], 1)
        self.assertEqual(tcp.attempts, [frame])
        self.assertTrue(first.closed and second.closed)

    def test_invalid_and_oversized_never_reach_socket(self):
        clock = Clock()
        valid = encode(synthetic(1, 1))
        serial = FakeSerial([b'x' * 300 + b'\n', b'not json\n', valid], clock)
        tcp = FakeSocket()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bridge(BridgeConfig('COM10', Path(temporary) / 'capture', max_frame=256, limit=1),
                                lambda *a, **k: serial, lambda *a, **k: tcp, clock, clock.sleep)
        self.assertEqual(result['invalid'], 1)
        self.assertEqual(result['oversized'], 1)
        self.assertEqual(tcp.attempts, [valid])

    def test_duration_bounds_disconnected_serial(self):
        clock = Clock()
        def no_serial(*args, **kwargs):
            raise OSError('not connected')
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bridge(BridgeConfig('COM10', Path(temporary) / 'capture', duration=2),
                                no_serial, clock=clock, sleep=clock.sleep)
        self.assertEqual(result['forwarded'], 0)
        self.assertEqual(result['serial_errors'], 2)
        self.assertEqual(result['elapsed_seconds'], 2)

    def test_duration_records_dangling_eof(self):
        clock = Clock()
        serial = FakeSerial([b'{"device_id":'], clock)
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bridge(BridgeConfig('COM10', Path(temporary) / 'capture', duration=0.3),
                                lambda *a, **k: serial, clock=clock, sleep=clock.sleep)
        self.assertEqual(result['incomplete'], 1)
        self.assertEqual(result['forwarded'], 0)

    def test_output_never_overwrites_previous_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / 'stats.json').write_text('preserve')
            with self.assertRaises(FileExistsError):
                run_bridge(BridgeConfig('COM10', output), lambda *a, **k: None)
            self.assertEqual((output / 'stats.json').read_text(), 'preserve')

    def test_adaptive_read_blocks_for_one_then_drains_bounded_available_bytes(self):
        clock = Clock()
        frame = encode(synthetic(0, 2))
        class AvailableSerial(FakeSerial):
            @property
            def in_waiting(self):
                return 0 if not self.read_sizes else 1000000
        serial = AvailableSerial([frame[:1], frame[1:]], clock)
        tcp = FakeSocket()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bridge(BridgeConfig('COM10', Path(temporary) / 'capture', limit=1),
                                lambda *a, **k: serial, lambda *a, **k: tcp, clock, clock.sleep)
        self.assertEqual(result['forwarded'], 1)
        self.assertEqual(serial.read_sizes, [1, 4096])

    def test_cli_returns_nonzero_when_duration_ends_below_requested_limit(self):
        fake_serial = types.SimpleNamespace(Serial=object())
        args = ['serial_bridge', '--port', 'COM10', '--output', 'unused', '--limit', '5', '--duration', '1']
        with patch('sys.argv', args), patch.dict('sys.modules', {'serial': fake_serial}), \
                patch('tools.serial_bridge.run_bridge', return_value={'forwarded': 3}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 1)

    def test_cli_duration_only_may_finish_with_no_frames(self):
        fake_serial = types.SimpleNamespace(Serial=object())
        args = ['serial_bridge', '--port', 'COM10', '--output', 'unused', '--duration', '1']
        with patch('sys.argv', args), patch.dict('sys.modules', {'serial': fake_serial}), \
                patch('tools.serial_bridge.run_bridge', return_value={'forwarded': 0}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(main())
    def test_configuration_rejects_unbounded_invalid_options(self):
        for field, value in [('max_frame', 0), ('max_frame', 262145), ('baud', 0),
                             ('tcp_port', 65536), ('duration', float('nan')),
                             ('reconnect_delay', 0), ('limit', -1)]:
            with self.subTest(field=field):
                config = BridgeConfig('COM10', Path('unused'))
                setattr(config, field, value)
                with self.assertRaises(ValueError):
                    config.validate()


if __name__ == '__main__':
    unittest.main()
