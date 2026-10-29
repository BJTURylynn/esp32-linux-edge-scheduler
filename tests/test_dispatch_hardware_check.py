import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.dispatch_hardware_check import run


def arguments(directory, **changes):
    values = dict(esp_host='192.0.2.10', uno_port='COM10', output=directory,
                  distribution='Ubuntu-24.04', policy='priority', web_port=0, timeout=300)
    values.update(changes)
    return argparse.Namespace(**values)


class HardwareHarnessGuards(unittest.TestCase):
    def test_process_failure_preserves_failure_and_never_calls_worker_or_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = arguments(Path(temp)/'missing-central')
            with patch('tools.dispatch_hardware_check.subprocess.Popen', side_effect=OSError('missing service')), \
                 patch('tools.dispatch_hardware_check.run_gateway') as gateway, \
                 patch('tools.dispatch_hardware_check.verify') as reference:
                result = run(selected)
            self.assertFalse(result['success'])
            self.assertFalse(result['hardware_validated'])
            self.assertIn('missing service', result['error'])
            gateway.assert_not_called()
            reference.assert_not_called()
            self.assertTrue((selected.output/'result.json').exists())

    def test_invalid_deadline_and_existing_output_do_not_start_service(self):
        with tempfile.TemporaryDirectory() as temp:
            selected = arguments(Path(temp)/'invalid', timeout=301)
            with patch('tools.dispatch_hardware_check.subprocess.Popen') as process:
                with self.assertRaises(ValueError):
                    run(selected)
                process.assert_not_called()
            self.assertFalse(selected.output.exists())
            selected = arguments(Path(temp))
            with patch('tools.dispatch_hardware_check.subprocess.Popen') as process:
                with self.assertRaises(FileExistsError):
                    run(selected)
                process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
