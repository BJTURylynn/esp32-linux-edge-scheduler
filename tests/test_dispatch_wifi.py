"""Wi-Fi setup uses injected USB/gateway transports; no real radio or secrets."""
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest

from tools.dispatch_wifi import EspWiFiManager, WiFiError


class Central:
    host = '127.0.0.1'
    port = 9100

    def __init__(self):
        self.node = dict(device='esp32c3-01', state='offline', task_id=None)
        self.queued = 0
        self.tasks = []
        self.down = False

    def state(self):
        if self.down:
            raise OSError('private transport text')
        return dict(nodes=[self.node], tasks=self.tasks, queue_depth=self.queued)


class FakeSerial:
    def __init__(self, *, status='connected', fail=False, blocked=False, raw_failure=False, **kwargs):
        self.kwargs = kwargs
        self.status, self.fail, self.blocked, self.raw_failure = status, fail, blocked, raw_failure
        self.port = None
        self.dtr = self.rts = True
        self.closed = False
        self.opened = threading.Event()
        self.messages = queue.Queue()
        self.writes = []
        self.stage = 0

    def open(self):
        if self.dtr or self.rts or self.kwargs['port'] is not None:
            raise AssertionError('DTR/RTS must be false before open')
        self.opened.set()

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True

    def read(self, maximum):
        if self.closed:
            raise OSError('closed')
        try:
            return self.messages.get(timeout=.02)
        except queue.Empty:
            return b''

    def write(self, data):
        self.writes.append(data)
        if self.raw_failure:
            raise OSError('private-password-must-not-leak')
        if self.blocked:
            return len(data)
        if data == b'STATUS\n':
            if self.status == 'old':
                self.messages.put(b'CONTROL_REJECTED reason=unknown_command\n')
            elif self.status == 'disconnected':
                self.messages.put(b'WIFI_STATUS state=disconnected ip=0.0.0.0 port=9001 rssi_dbm=0 protocol=2\n')
            else:
                # Split reads and unrelated diagnostics cannot leak into state.
                self.messages.put(b'unknown diagnostic contains private-password-must-not-leak\nWIFI_STATUS state=conn')
                self.messages.put(b'ected ip=192.168.1.33 port=9001 rssi_dbm=-47 protocol=2\r\n')
        elif data == b'CONFIG\n':
            self.stage = 1
            self.messages.put(b'CONFIG_SSID\n')
        elif self.stage == 1:
            self.stage = 2
            self.messages.put(b'CONFIG_PASSWORD\n')
        elif self.stage == 2:
            self.stage = 3
            self.messages.put(b'WIFI_FAILED status=4 waiting=CONFIG\n' if self.fail else
                              b'WIFI_READY ip=192.168.1.33 port=9001 rssi_dbm=-51 channel=6 connect_ms=100\n')
        return len(data)


class WiFiManagerTests(unittest.TestCase):
    def make(self, **options):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name) / 'wifi'
        central = Central()
        calls, channels = [], []
        def gateway(device, output, *, stop, **kwargs):
            output.mkdir()
            calls.append((device, output, kwargs, stop))
            stop.wait(20)
        serial_options = options.pop('serial_options', {})
        def serial_factory(**kwargs):
            channel = FakeSerial(**kwargs, **serial_options)
            channels.append(channel)
            return channel
        manager = EspWiFiManager(central, root, gateway_runner=gateway,
            serial_factory=serial_factory, uno_port='COM10',
            port_provider=lambda: [dict(port='COM10', description='Uno'),
                                   dict(port='COM11', description='USB JTAG serial'),
                                   dict(port='COM3', description='Standard Serial over Bluetooth link')],
            **options)
        self.addCleanup(manager.close)
        return manager, central, root, calls, channels

    def completed(self, manager, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = manager.snapshot()
            if not state['busy']:
                return state
            time.sleep(.01)
        self.fail('operation did not complete')

    def wait_calls(self, calls, amount):
        deadline = time.monotonic() + 2
        while len(calls) < amount and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(calls), amount)

    def test_constructor_and_snapshot_are_side_effect_free_unknown(self):
        manager, central, root, calls, channels = self.make()
        self.assertFalse(root.exists())
        self.assertEqual(manager.snapshot()['observation']['state'], 'unknown')
        self.assertEqual(calls, [])
        self.assertEqual(channels, [])
        self.assertEqual(manager.ports()['ports'], [dict(port='COM11', description='USB JTAG serial')])

    def test_read_only_probe_distinguishes_wifi_and_does_not_switch_gateway(self):
        manager, central, root, calls, channels = self.make(host='192.168.1.2')
        manager.start()
        self.wait_calls(calls, 1)
        manager.probe({'port': 'COM11'})
        state = self.completed(manager)
        self.assertEqual(state['observation']['state'], 'connected')
        self.assertEqual(state['observation']['source'], 'usb')
        self.assertEqual(state['observation']['ip'], '192.168.1.33')
        self.assertEqual(state['observation']['rssi_dbm'], -47)
        self.assertGreater(state['observation']['observed_at'], time.time()-5)
        self.assertEqual(state['host'], '192.168.1.2')
        self.assertFalse(calls[0][3].is_set())
        self.assertEqual(channels[0].writes, [b'STATUS\n'])
        self.assertNotIn('private-password', json.dumps(state))
        self.assertTrue(channels[0].closed)

    def test_disconnected_and_old_firmware_are_honest(self):
        for status, expected in [('disconnected', 'disconnected'), ('old', 'unknown')]:
            with self.subTest(status=status):
                manager, _, _, _, channels = self.make(serial_options={'status': status})
                manager.probe({'port': 'COM11'})
                state = self.completed(manager)
                self.assertEqual(state['observation']['state'], expected)
                self.assertNotIn(b'CONFIG\n', channels[0].writes)
                if status == 'old':
                    self.assertIn('旧固件', state['message'])

    def test_successful_config_replaces_only_owned_relay_and_keeps_secrets_out(self):
        manager, central, root, calls, channels = self.make(host='192.168.1.2')
        manager.start()
        self.wait_calls(calls, 1)
        manager.connect({'port': 'COM11', 'ssid': 'test-net', 'password': 'private-password'})
        state = self.completed(manager)
        self.wait_calls(calls, 2)
        self.assertTrue(calls[0][3].is_set())
        self.assertFalse(calls[1][3].is_set())
        self.assertEqual(calls[1][2]['esp_host'], '192.168.1.33')
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertEqual(channels[0].writes, [b'CONFIG\n', b'test-net\n', b'private-password\n'])
        self.assertEqual(state['observation']['state'], 'connected')
        self.assertNotIn('private-password', json.dumps(state))
        self.assertNotIn('test-net', json.dumps(state))
        for path in root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private-password', path.read_bytes())

    def test_failed_config_never_restarts_stale_host(self):
        manager, central, root, calls, channels = self.make(host='192.168.1.2', serial_options={'fail': True})
        manager.start()
        self.wait_calls(calls, 1)
        manager.connect({'port': 'COM11', 'ssid': 'test-net', 'password': 'private-password'})
        state = self.completed(manager)
        self.assertEqual(state['phase'], 'error')
        self.assertEqual(state['observation']['state'], 'disconnected')
        self.assertIsNone(state['host'])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][3].is_set())

    def test_failed_operation_stays_exclusive_until_usb_cleanup_finishes(self):
        manager, _, _, calls, _ = self.make(serial_options={'raw_failure': True})
        entered, release = threading.Event(), threading.Event()
        close = manager._close_channel
        def held_close():
            entered.set()
            release.wait(3)
            close()
        manager._close_channel = held_close
        try:
            manager.probe({'port': 'COM11'})
            self.assertTrue(entered.wait(2))
            self.assertTrue(manager.snapshot()['busy'])
            with self.assertRaises(WiFiError) as error:
                manager.attach({'host': '192.168.1.33'})
            self.assertEqual(error.exception.status, 409)
            with self.assertRaises(WiFiError):
                with manager.task_guard():
                    self.fail('task mutation must stay blocked during cleanup')
            self.assertEqual(calls, [])
        finally:
            release.set()
        self.assertFalse(self.completed(manager)['busy'])

    def test_attach_offline_node_can_drain_existing_queue_while_uno_is_busy(self):
        manager, central, _, calls, channels = self.make()
        central.queued = 12
        central.tasks = [dict(device='uno-01', state='running')]
        manager.attach({'host': '192.168.1.33'})
        state = self.completed(manager)
        self.wait_calls(calls, 1)
        self.assertEqual(state['host'], '192.168.1.33')
        self.assertEqual(state['observation']['state'], 'unknown')
        self.assertEqual(channels, [])

    def test_inflight_esp_or_idle_with_backlog_cannot_reconfigure(self):
        for device_state, queued in [('running', 0), ('dispatching', 0), ('idle', 1)]:
            with self.subTest(device_state=device_state):
                manager, central, _, calls, channels = self.make()
                central.node['state'] = device_state
                central.queued = queued
                with self.assertRaises(WiFiError) as error:
                    manager.attach({'host': '192.168.1.33'})
                self.assertEqual(error.exception.status, 409)
                self.assertEqual(calls, [])
                self.assertEqual(channels, [])

    def test_invalid_fields_credential_lengths_ports_and_addresses_are_rejected(self):
        manager, _, root, calls, channels = self.make()
        for body in ({'host':'8.8.8.8'}, {'host':'127.0.0.1'}, {'host':'0.0.0.0'},
                     {'host':'224.0.0.1'}, {'host':'192.168.1.33','path':'private'}):
            with self.subTest(body=body), self.assertRaises(WiFiError):
                manager.attach(body)
        for extra in ({'port':'COM10'}, {'port':'COM3'}, {'port':'../../private'},
                      {'password':'short'}, {'ssid':'name\nCONFIG'}, {'password':'private\rCONFIG'},
                      {'ssid':'界'*11}, {'password':'x'*64}, {'password':None}):
            body = dict(port='COM11',ssid='test-net',password='private-password')
            body.update(extra)
            with self.subTest(extra=extra), self.assertRaises(WiFiError):
                manager.connect(body)
        self.assertFalse(root.exists())
        self.assertEqual(calls, [])
        self.assertEqual(channels, [])

    def test_busy_guard_and_shutdown_cancel_pending_usb_without_resurrection(self):
        manager, central, _, calls, channels = self.make(serial_options={'blocked': True})
        manager.connect(dict(port='COM11',ssid='test-net',password='private-password'))
        with self.assertRaises(WiFiError) as error:
            with manager.task_guard():
                self.fail('busy mutation was allowed')
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(WiFiError):
            manager.probe({'port':'COM11'})
        manager.close()
        self.assertTrue(manager.snapshot()['closed'])
        self.assertEqual(calls, [])
        self.assertTrue(channels[0].closed)
        with self.assertRaises(WiFiError):
            manager.attach({'host':'192.168.1.33'})

    def test_central_failure_and_raw_serial_error_do_not_leak(self):
        manager, central, _, _, _ = self.make(serial_options={'raw_failure':True})
        central.down = True
        with self.assertRaises(WiFiError) as error:
            manager.attach({'host':'192.168.1.33'})
        self.assertEqual(error.exception.status, 503)
        self.assertNotIn('private', str(error.exception))
        central.down = False
        manager.probe({'port':'COM11'})
        state = self.completed(manager)
        self.assertEqual(state['phase'], 'error')
        self.assertNotIn('private-password', json.dumps(state))


if __name__ == '__main__':
    unittest.main()
