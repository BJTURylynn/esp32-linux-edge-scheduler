"""The opt-in live runner is tested offline; CI never calls a real API."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.agent_live import run_live
from tools.llm import ModelError


class DemoClient:
    def __init__(self, *, api_key, **kwargs):
        self.key = api_key
        self.calls = 0
        self.last_response_metadata = {}

    def complete(self, messages, tools):
        self.calls += 1
        self.last_response_metadata = {"status": 200, "model": "demo-model",
                                       "id": "response-" + self.key,
                                       "finish_reason": "tool_calls" if self.calls == 1 else "stop"}
        if self.calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": "one", "type": "function", "function": {"name": "read_report", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "本轮合成负载中紧急等待降低；不能据此证明吞吐提高。"}


class FailureClient(DemoClient):
    def complete(self, messages, tools):
        raise ModelError("Model API HTTP error (403).")


class LiveRunnerTests(unittest.TestCase):
    def fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        status, report = root / "status.json", root / "report.json"
        status.write_text(json.dumps({"policy": "priority", "queue_depth": 0,
            "queue_capacity": 64, "accepted": 40, "completed": 40, "rejected": 0,
            "running": False, "active": False, "accepting": False,
            "window": 8, "passes": 1, "fatal": False}))
        def metric(value):
            return {"completed": 4, "rejected": 0,
                    "wait_ms": {"mean": value, "p95": value, "max": value},
                    "total_ms": {"mean": value + 1, "p95": value + 1, "max": value + 1}}
        report.write_text(json.dumps({"schema_version": 1, "data_source": "synthetic",
            "p95_method": "nearest_rank", "comparable_configuration": True,
            "pooled": {"fifo": {"urgent": metric(100), "normal": metric(50)},
                       "priority": {"urgent": metric(10), "normal": metric(60)}}}))
        return dict(question="解释报告", api_key="demo-secret", status=status,
                    report=report, output=root / "live", client_factory=DemoClient)

    def test_two_calls_and_real_file_tool_make_success_without_storing_key(self):
        args = self.fixture()
        result = run_live(**args)
        self.assertTrue(result["success"])
        self.assertEqual(result["agent"]["mode"], "llm")
        self.assertEqual(result["agent"]["model_calls"], 2)
        self.assertEqual(result["agent"]["tool_trace"], [{"tool": "read_report", "available": True}])
        self.assertEqual(len(result["api_trace"]), 2)
        for path in args["output"].iterdir():
            self.assertNotIn("demo-secret", path.read_text())
        self.assertEqual(len(result["evidence_sha256"]["report"]), 64)

    def test_fallback_is_recorded_as_failure_not_live_success(self):
        args = self.fixture()
        args["client_factory"] = FailureClient
        result = run_live(**args)
        self.assertFalse(result["success"])
        self.assertEqual(result["agent"]["mode"], "fallback")
        self.assertEqual(result["api_trace"], [])
        self.assertIn("403", result["agent"]["reason"])

    def test_existing_evidence_is_never_overwritten_or_called_again(self):
        args = self.fixture()
        args["output"].mkdir()
        saved = args["output"] / "sentinel"
        saved.write_text("keep")
        with self.assertRaises(FileExistsError):
            run_live(**args)
        self.assertEqual(saved.read_text(), "keep")

    def test_one_allowed_tool_with_missing_evidence_is_not_success(self):
        args = self.fixture()
        args["report"].unlink()
        result = run_live(**args)
        self.assertFalse(result["success"])
        self.assertEqual(result["agent"]["mode"], "fallback")


if __name__ == "__main__":
    unittest.main()
