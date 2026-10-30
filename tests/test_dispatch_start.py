import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.dispatch_start import run, supervisor_command, validate
from tools.experiment import free_port


ROOT = Path(__file__).resolve().parents[1]
LEGACY = Path(os.environ.get('EDGE_SERVER', ROOT/'linux/build/edge_scheduler'))
BINARY = Path(os.environ.get('EDGE_DISPATCHER', LEGACY.with_name('edge_dispatcher')))


def args(output, **changes):
    result = dict(esp_host='192.0.2.10', uno_port='COM10', output=output,
                  policy='priority', distribution='Ubuntu-24.04', web_port=free_port())
    result.update(changes)
    return argparse.Namespace(**result)


class DispatchStartTests(unittest.TestCase):
    def test_can_start_without_devices_and_configure_wifi_from_page_later(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = args(Path(temp)/'run', esp_host=None, uno_port=None)
            validate(selected)
            self.assertFalse(selected.output.exists())

    def test_esp_usb_port_cannot_be_uno_port(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                validate(args(Path(temp)/'run', esp_serial='COM10'))

    def test_invalid_start_configuration_fails_before_touching_output(self):
        for change in ({'web_port': 0}, {'esp_host': '::1'}, {'esp_host': '255.255.255.255'}, {'uno_port': ''},
                       {'distribution': '--invalid'}, {'policy': 'automatic'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                selected = args(Path(temp)/'run', **change)
                with self.assertRaises(ValueError):
                    validate(selected)
                self.assertFalse(selected.output.exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = args(Path(temp))
            with patch('tools.dispatch_start.subprocess.Popen') as launch:
                with self.assertRaises(FileExistsError):
                    run(selected)
                launch.assert_not_called()

    def test_foreign_web_listener_is_not_stopped_and_no_children_started(self):
        with tempfile.TemporaryDirectory() as temp, socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            selected = args(Path(temp)/'run', web_port=listener.getsockname()[1])
            with patch('tools.dispatch_start.subprocess.Popen') as launch:
                with self.assertRaises(OSError):
                    run(selected)
                launch.assert_not_called()
            self.assertFalse(selected.output.exists())
            with socket.create_connection(('127.0.0.1', selected.web_port), timeout=.2):
                pass


@unittest.skipUnless(os.name != 'nt' and BINARY.exists(), 'needs Linux C dispatcher')
class DispatchStartLifecycleTests(unittest.TestCase):
    def command(self, selected, root, port):
        return supervisor_command(selected, root, port) + [
            '--base', str(selected.output.parent/'native'), '--server', str(BINARY.resolve())]

    @staticmethod
    def gateway(device, output, *, stop, **unused):
        # A deliberately inert relay: this verifies lifetime, not fake hardware.
        output.mkdir()
        stop.wait(15)

    def test_stop_and_eof_cleanup_real_owned_c_service_without_task_success_claim(self):
        for control in ('STOP\n', ''):
            with self.subTest(control=control), tempfile.TemporaryDirectory() as temp:
                selected = args(Path(temp)/'dispatch-run')
                with contextlib.redirect_stdout(io.StringIO()):
                    result = run(selected, gateway_runner=self.gateway,
                                 command_factory=self.command, control=io.StringIO(control))
                self.assertTrue(result['success'], result)
                self.assertFalse(result['hardware_validated'])
                self.assertEqual(result['last_live_state']['tasks'], [])
                state = json.loads((selected.output/'service/status.json').read_text())
                self.assertFalse(state['running'])
                for port in (selected.web_port, result['central_port']):
                    with self.assertRaises(OSError):
                        socket.create_connection(('127.0.0.1', port), timeout=.2)

    def test_supervisor_sigterm_archives_and_releases_owned_listener(self):
        import signal
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            port = free_port()
            process = subprocess.Popen([sys.executable, '-u', '-m', 'tools.dispatch_service',
                '--name', 'signal-run', '--port', str(port), '--copy-to', str(directory/'service'),
                '--base', str(directory/'native'), '--server', str(BINARY.resolve())],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            try:
                self.assertEqual(json.loads(process.stdout.readline())['event'], 'ready')
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                self.assertIn('"event": "finished"', output)
                self.assertFalse(json.loads((directory/'service/status.json').read_text())['running'])
                with self.assertRaises(OSError):
                    socket.create_connection(('127.0.0.1', port), timeout=.2)
            finally:
                if process.poll() is None:
                    process.kill(); process.communicate()


if __name__ == '__main__':
    unittest.main()
