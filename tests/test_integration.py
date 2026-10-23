"""Black-box tests against real POSIX C service; set EDGE_SERVER to enable."""
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
from tools.protocol import encode, reference, synthetic


@unittest.skipUnless(os.environ.get("EDGE_SERVER"), "set EDGE_SERVER to the compiled Linux executable")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.process = None
        self.log = None

    def tearDown(self):
        try:
            if self.process:
                stop(self.process)
        finally:
            if self.log:
                self.log.close()
            self.temp.cleanup()

    def start(self, policy="fifo", capacity=32, passes=1):
        self.port = free_port()
        self.log = (self.directory / "console.log").open("w")
        self.process = subprocess.Popen([os.environ["EDGE_SERVER"], "--bind", "127.0.0.1",
            "--port", str(self.port), "--policy", policy, "--capacity", str(capacity),
            "--window", "3", "--passes", str(passes), "--output", str(self.directory)],
            stdout=self.log, stderr=subprocess.STDOUT)
        wait_ready(self.process, self.directory / "status.json")
        for attempt in range(100):
            try:
                with self.connect():
                    return
            except OSError:
                if attempt == 99:
                    raise
                time.sleep(.01)

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=10)

    def finish(self, expected):
        state = wait_complete(self.process, self.directory, expected, 60)
        stop(self.process)
        self.assertEqual(self.process.returncode, 0, (self.directory / "console.log").read_text())
        with (self.directory / "tasks.csv").open(newline="") as handle:
            return state, list(csv.DictReader(handle))

    def test_fragment_coalesce_reconnect_and_numerics(self):
        self.start()
        messages = [synthetic(i, n) for i, n in enumerate((1, 7, 8192, 31))]
        with self.connect() as sock:
            frame = encode(messages[0])
            for byte in frame:
                sock.sendall(bytes([byte]))
            sock.sendall(encode(messages[1]) + encode(messages[2]))
        with self.connect() as sock:
            sock.sendall(encode(messages[3]))
        state, rows = self.finish(4)
        self.assertEqual(state["completed"], 4)
        self.assertEqual({int(r["seq"]) for r in rows}, {0, 1, 2, 3})
        by_seq = {int(row["seq"]): row for row in rows}
        for message in messages:
            row = by_seq[message["seq"]]
            expected = reference(message["samples"], 3)
            for metric in ("rms", "mean"):
                self.assertAlmostEqual(float(row[metric]), expected[metric], delta=1e-8)
            self.assertGreaterEqual(float(row["total_ms"]), float(row["compute_ms"]))

    def test_bad_frames_do_not_poison_next_connection(self):
        self.start()
        bad = [b'{"broken":1}\n', b'{"device_id":"x","seq":1,"priority":"urgent","source":"synthetic","samples":[NaN]}\n',
               b'x'*262145+b'\n', b'{"partial":']
        for frame in bad:
            with self.connect() as sock:
                sock.sendall(frame)
        with self.connect() as sock:
            sock.sendall(encode(synthetic(10, 4)))
        state, rows = self.finish(5)
        self.assertEqual(state["rejected"], 4)
        self.assertEqual(state["completed"], 1)
        self.assertEqual(len(rows), 5)

    def test_queue_overload_is_recorded_and_bounded(self):
        self.start(capacity=1, passes=10000)
        with self.connect() as sock:
            sock.sendall(b''.join(encode(synthetic(i, 8192)) for i in range(20)))
        state, rows = self.finish(20)
        self.assertGreater(state["rejected"], 0)
        self.assertEqual(state["accepted"], state["completed"])
        self.assertEqual(state["queue_capacity"], 1)
        self.assertTrue(any(r["reason"] == "queue_full" for r in rows))

    def test_priority_selects_waiting_urgent_without_preemption(self):
        self.start(policy="priority", passes=10000)
        with self.connect() as sock:
            sock.sendall(encode(synthetic(0, 8192)))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = json.loads((self.directory / "status.json").read_text())
                if state["active"]:
                    break
                time.sleep(.001)
            else:
                self.fail("could not observe active normal task")
            sock.sendall(encode(synthetic(1, 8192)) + encode(synthetic(2, 8, "urgent")) + encode(synthetic(3, 8, "urgent")))
        _, rows = self.finish(4)
        self.assertEqual([int(r["seq"]) for r in rows], [0, 2, 3, 1])


if __name__ == "__main__":
    unittest.main()
