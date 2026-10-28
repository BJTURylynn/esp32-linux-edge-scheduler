"""UNO-shaped serial frames through the bridge into a real Linux C service."""
import csv
from collections import deque
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

from tools.experiment import free_port, stop, wait_complete, wait_ready
from tools.protocol import encode, reference
from tools.serial_bridge import BridgeConfig, run_bridge


def uno_message(seq):
    urgent = seq % 5 == 4
    count = 16 if urgent else 256
    return {"device_id": "uno-01", "seq": seq,
            "priority": "urgent" if urgent else "normal", "source": "synthetic",
            "samples": [((index + (seq % 201)) % 201) - 100 for index in range(count)]}


class ChunkedSerial:
    """Respect read sizes while deliberately splitting and coalescing lines."""
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, count):
        if not self.chunks:
            return b""
        chunk = self.chunks[0]
        result = chunk[:count]
        if len(result) == len(chunk):
            self.chunks.popleft()
        else:
            self.chunks[0] = chunk[count:]
        return result

    def close(self):
        self.closed = True


@unittest.skipUnless(os.environ.get("EDGE_SERVER"), "set EDGE_SERVER to the compiled Linux executable")
class SerialDeviceIntegrationTests(unittest.TestCase):
    def test_uno_frames_are_filtered_forwarded_and_computed(self):
        with tempfile.TemporaryDirectory(prefix="edge-serial-") as temporary:
            root = Path(temporary)
            service = root / "service"
            bridge = root / "bridge"
            port = free_port()
            process = None
            with (root / "server.log").open("w", encoding="utf-8") as log:
                try:
                    process = subprocess.Popen([
                        os.environ["EDGE_SERVER"], "--bind", "127.0.0.1", "--port", str(port),
                        "--policy", "priority", "--capacity", "32", "--window", "3",
                        "--passes", "1", "--output", str(service)],
                        stdout=log, stderr=subprocess.STDOUT)
                    wait_ready(process, service / "status.json")
                    # Confirm TCP admission rather than assume the status write means listen-ready.
                    deadline = time.monotonic() + 5
                    while True:
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=1):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(.01)

                    messages = [uno_message(seq) for seq in range(10)]
                    # Serial firmware emits CRLF; the bridge canonicalizes it to LF.
                    frames = [encode(message).replace(b"\n", b"\r\n") for message in messages]
                    serial = ChunkedSerial([
                        b'{"broken":1}\n', b'x' * 4096, b'x' * 4096, b'x\n',
                        frames[0][:7], frames[0][7:31],
                        frames[0][31:] + b''.join(frames[1:])])
                    config = BridgeConfig(port="fake-uno", output=bridge, tcp_port=port,
                                          limit=10, duration=5)
                    stats = run_bridge(config, lambda *args, **kwargs: serial)
                    self.assertTrue(serial.closed)
                    self.assertEqual(stats["forwarded"], 10)
                    self.assertEqual(stats["invalid"], 1)
                    self.assertEqual(stats["oversized"], 1)
                    self.assertEqual(stats["dropped"], 0)
                    self.assertEqual(stats["uncertain"], 0)
                    self.assertEqual(stats["incomplete"], 0)
                    self.assertFalse(stats["running"])
                    state = wait_complete(process, service, 10, 10)
                    self.assertEqual(state["completed"], 10)
                    self.assertEqual(state["rejected"], 0)
                    stop(process)
                    self.assertEqual(process.returncode, 0)
                    with (service / "tasks.csv").open(newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(len(rows), 10)
                    self.assertEqual({row["priority"] for row in rows}, {"normal", "urgent"})
                    self.assertEqual(sum(row["priority"] == "urgent" for row in rows), 2)
                    by_sequence = {int(row["seq"]): row for row in rows}
                    self.assertEqual(set(by_sequence), set(range(10)))
                    for message in messages:
                        row = by_sequence[message["seq"]]
                        self.assertEqual(row["device_id"], "uno-01")
                        self.assertEqual(row["source"], "synthetic")
                        self.assertEqual(row["status"], "completed")
                        self.assertEqual(int(row["samples"]), len(message["samples"]))
                        expected = reference(message["samples"], window=3)
                        for metric in ("rms", "mean"):
                            self.assertAlmostEqual(float(row[metric]), expected[metric], delta=1e-8)
                    captured = [json.loads(line) for line in (bridge / "frames.jsonl").read_text().splitlines()]
                    self.assertEqual(captured, messages)
                    saved_stats = json.loads((bridge / "stats.json").read_text())
                    self.assertEqual(saved_stats["forwarded"], 10)
                    self.assertFalse(saved_stats["running"])
                finally:
                    if process is not None:
                        stop(process)


if __name__ == "__main__":
    unittest.main()
