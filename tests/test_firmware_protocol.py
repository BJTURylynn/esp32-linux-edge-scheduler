"""Send the exact C firmware encoder output into the real Linux server."""
import csv
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

from tools.experiment import free_port, stop, wait_complete, wait_ready
from tools.protocol import reference


@unittest.skipUnless(os.environ.get("EDGE_SERVER") and os.environ.get("EDGE_FRAME_TEST"),
                     "set EDGE_SERVER and EDGE_FRAME_TEST to the compiled C executables")
class FirmwareProtocolTests(unittest.TestCase):
    def test_maximum_firmware_frame_is_accepted_and_computed(self):
        generated = subprocess.run([os.environ["EDGE_FRAME_TEST"], "--emit-max"],
                                   capture_output=True, check=True, timeout=10)
        frame = generated.stdout
        message = json.loads(frame)
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertTrue(frame.endswith(b"\n"))
        self.assertNotIn(b"\x00", frame)
        self.assertLessEqual(len(frame), 262144)
        self.assertEqual(message["seq"], 9007199254740991)
        self.assertEqual(message["priority"], "urgent")
        self.assertEqual(message["source"], "synthetic")
        self.assertEqual(len(message["device_id"]), 32)
        self.assertEqual(len(message["samples"]), 8192)
        # Independently verify C's generator rather than just accepting valid JSON.
        self.assertEqual(message["samples"],
                         [(message["seq"] % 2001 + i * 37) % 2001 - 1000
                          for i in range(8192)])
        with tempfile.TemporaryDirectory(prefix="firmware-compat-") as folder:
            output = Path(folder)
            port = free_port()
            with (output / "console.log").open("w") as log:
                process = subprocess.Popen([os.environ["EDGE_SERVER"], "--bind", "127.0.0.1",
                    "--port", str(port), "--output", str(output), "--window", "3"],
                    stdout=log, stderr=subprocess.STDOUT)
                try:
                    wait_ready(process, output / "status.json")
                    for attempt in range(100):
                        try:
                            client = socket.create_connection(("127.0.0.1", port), timeout=10)
                            break
                        except OSError:
                            if attempt == 99:
                                raise
                            time.sleep(.01)
                    with client:
                        # Firmware send_all may split writes at arbitrary boundaries.
                        for begin in range(0, len(frame), 137):
                            client.sendall(frame[begin:begin + 137])
                    state = wait_complete(process, output, 1, 30)
                    self.assertEqual(state["completed"], 1)
                    self.assertEqual(state["rejected"], 0)
                finally:
                    stop(process)
                self.assertEqual(process.returncode, 0)
            with (output / "tasks.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["device_id"], message["device_id"])
            self.assertEqual(int(row["seq"]), message["seq"])
            self.assertEqual(row["priority"], "urgent")
            expected = reference(message["samples"], 3)
            for metric in ("rms", "mean"):
                self.assertAlmostEqual(float(row[metric]), expected[metric], delta=1e-8)


if __name__ == "__main__":
    unittest.main()
