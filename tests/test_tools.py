import json
import math
from pathlib import Path
import tempfile
import unittest
from tools.agent import ReadOnlyAgent
from tools.protocol import encode, reference, synthetic, validate_message
from tools.replay import generate, load
from tools.report import distribution, summarize


class ToolTests(unittest.TestCase):
    def test_reference_known_answer(self):
        # [2,4,6,8], width 2 => [2,3,5,7]
        self.assertEqual(reference([2, 4, 6, 8], 2)["mean"], 4.25)
        self.assertAlmostEqual(reference([2, 4, 6, 8], 2)["rms"], math.sqrt(87/4))

    def test_protocol_limits(self):
        self.assertTrue(encode(synthetic(0, 8192)).endswith(b"\n"))
        for field, value in (("seq", True), ("seq", 2**53), ("device_id", "a,b"),
                             ("samples", []), ("samples", [float("nan")]),
                             ("samples", [True]), ("samples", [1e7]), ("source", "sensor")):
            with self.subTest(field=field, value=value):
                message = synthetic(0, 1)
                message[field] = value
                with self.assertRaises(ValueError):
                    validate_message(message)

    def test_synthetic_workload_is_repeatable(self):
        self.assertEqual(generate(), generate())
        self.assertEqual(len(generate()), 32)

    def test_nearest_rank_and_missing_observations(self):
        self.assertEqual(distribution(list(range(1, 101)))["p95"], 95)
        self.assertIsNone(distribution([])["p95"])
        rows = [{"priority": "urgent", "status": "rejected", "wait_ms": "", "total_ms": "", "compute_ms": ""}]
        metrics = summarize(rows)["urgent"]
        self.assertEqual(metrics["rejected"], 1)
        self.assertEqual(metrics["completed"], 0)
        self.assertIsNone(metrics["wait_ms"]["p95"])

    def test_schedule_rejects_negative_and_nan(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "load.jsonl"
            for at in (-1, float("nan")):
                path.write_text(json.dumps({"at_ms": at, "message": synthetic(0, 1)}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load(path)

    def test_agent_missing_and_tool_allowlist(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.json"
            agent = ReadOnlyAgent(path, path)
            self.assertFalse(agent.call("query_status")["available"])
            self.assertIn("无法判断", agent.explain())
            with self.assertRaises(ValueError):
                agent.call("run_shell")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
