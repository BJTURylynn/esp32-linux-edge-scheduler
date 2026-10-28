"""Exercise the real C server over TCP; requires a POSIX/Linux build."""
from __future__ import annotations

import csv
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

BINARY = Path(os.environ.get("EDGE_SERVER", str(Path(__file__).resolve().parents[1] / "linux" / "build" / "edge_scheduler"))).resolve()


def message(seq: int, priority: str = "normal", count: int = 4) -> bytes:
    return (json.dumps({"device_id": "test-01", "seq": seq, "priority": priority,
                        "source": "synthetic", "samples": [i % 4 + 1 for i in range(count)]},
                       separators=(",", ":")) + "\n").encode()


class RunningServer:
    def __init__(self, root: Path, policy: str = "fifo", capacity: int = 32,
                 passes: int = 1):
        self.output = root / "out"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.log = (root / "server.log").open("w+")
        self.process = subprocess.Popen(
            [str(BINARY), "--port", str(self.port), "--policy", policy, "--capacity",
             str(capacity), "--window", "2", "--passes", str(passes), "--output", str(self.output)],
            stdout=self.log, stderr=self.log)
        self.wait(lambda state: state["running"])

    def state(self):
        try:
            # Atomic replacement means JSON decoding errors are test failures.
            return json.loads((self.output / "status.json").read_text())
        except FileNotFoundError:
            return None

    def wait(self, condition, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state is not None and condition(state):
                return state
            if self.process.poll() is not None:
                self.log.seek(0)
                raise AssertionError(f"server exited {self.process.returncode}: {self.log.read()}")
            time.sleep(0.005)
        raise AssertionError(f"server state timed out: {self.state()}")

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def rows(self):
        with (self.output / "tasks.csv").open(newline="") as file:
            return list(csv.DictReader(file))

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        result = self.process.wait(timeout=40)
        self.log.close()
        return result

    def cleanup(self):
        try:
            self.stop()
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            self.log.close()


@unittest.skipUnless(os.environ.get("EDGE_SERVER"), "set EDGE_SERVER to the compiled Linux executable")
class LifecycleTests(unittest.TestCase):
    def start(self, **options):
        folder = tempfile.TemporaryDirectory(prefix="edge-test-")
        self.addCleanup(folder.cleanup)
        server = RunningServer(Path(folder.name), **options)
        self.addCleanup(server.cleanup)
        return server

    def test_queue_bound_and_shutdown_drain(self):
        server = self.start(capacity=1, passes=40000)
        with server.connect() as client:
            client.sendall(message(0, count=8192))
            server.wait(lambda s: s["active"])
            client.sendall(message(1) + message(2) + message(3))
            server.wait(lambda s: s["rejected"] == 2)
            # The initial computation is deliberately still running here.
            server.process.send_signal(signal.SIGTERM)
            draining = server.wait(lambda s: not s["accepting"])
            self.assertTrue(draining["running"])
            self.assertTrue(draining["active"])
            self.assertEqual((draining["accepted"], draining["completed"],
                              draining["queue_depth"]), (2, 0, 1))
            self.assertEqual(server.stop(), 0)
        final = server.state()
        self.assertEqual((final["accepted"], final["completed"], final["rejected"]), (2, 2, 2))
        self.assertEqual(final["queue_depth"], 0)
        rejected = [r for r in server.rows() if r["status"] == "rejected"]
        self.assertTrue(all(r["reason"] == "queue_full" for r in rejected))

    def test_invalid_cli_is_rejected(self):
        for args in (("--capacity", "0"), ("--port", "65536"), ("--window", "8193"),
                     ("--passes", "0"), ("--policy", "random"), ("--bind", "localhost"),
                     ("--unknown", "1"), ("--port",)):
            with self.subTest(args=args):
                result = subprocess.run([str(BINARY), *args], capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)

    def test_existing_csv_is_preserved(self):
        server = self.start()
        self.assertEqual(server.stop(), 0)
        before = (server.output / "tasks.csv").read_bytes()
        result = subprocess.run([str(BINARY), "--port", str(server.port), "--output", str(server.output)],
                                capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((server.output / "tasks.csv").read_bytes(), before)

    def test_status_write_failure_is_not_success(self):
        server = self.start()
        status = server.output / "status.json"
        status.unlink()
        status.mkdir()
        with server.connect() as client:
            client.sendall(message(1))
        self.assertNotEqual(server.process.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
