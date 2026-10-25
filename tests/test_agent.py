"""Offline evidence and bounded tool-loop regressions; no paid API calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.agent import ReadOnlyAgent
from tools.llm import ModelError


def fixture_report():
    def row(p95):
        return {"completed": 3, "rejected": 1,
                "wait_ms": {"mean": p95, "p95": p95, "max": p95},
                "total_ms": {"mean": p95 + 1, "p95": p95 + 1, "max": p95 + 1}}
    return {"schema_version": 1, "data_source": "synthetic", "p95_method": "nearest_rank",
            "comparable_configuration": True,
            "pooled": {"fifo": {"urgent": row(100), "normal": row(50)},
                       "priority": {"urgent": row(10), "normal": row(60)}},
            "metadata": {"private": "MUST_NOT_BE_SENT_TO_MODEL"}}


def tool(name="read_report", args="{}", ident="call-1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": ident, "type": "function", "function": {"name": name, "arguments": args}}]}


def answer(text="紧急任务的等待下降，普通任务等待上升；这是该次合成数据实验中的取舍。"):
    return {"role": "assistant", "content": text}


class FakeClient:
    def __init__(self, responses):
        self.responses, self.requests = iter(responses), []

    def complete(self, messages, tools):
        self.requests.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / "status.json"
        self.report = self.root / "report.json"
        self.status.write_text(json.dumps({"policy": "priority", "queue_depth": 1,
            "queue_capacity": 32, "accepted": 4, "completed": 2, "rejected": 1,
            "window": 8, "passes": 1, "running": True, "active": True,
            "accepting": False, "fatal": False}), encoding="utf-8")
        self.report.write_text(json.dumps(fixture_report()), encoding="utf-8")
        self.agent = ReadOnlyAgent(self.status, self.report)

    def test_offline_uses_recorded_values_and_does_not_write(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.status, self.report)]
        result = self.agent.ask("为什么紧急任务变快了？")
        self.assertEqual(result["mode"], "rules")
        self.assertEqual(result["model_calls"], 0)
        self.assertIn("100.000 ms → priority 10.000 ms", result["answer"])
        self.assertIn("50.000 ms → priority 60.000 ms", result["answer"])
        self.assertIn("不是进程存活检查", result["evidence_text"])
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.status, self.report)])

    def test_model_selects_tool_and_receives_only_validated_metrics(self):
        client = FakeClient([tool(), answer()])
        result = self.agent.ask("解释调度差异", client)
        self.assertEqual(result["mode"], "llm")
        self.assertEqual(result["model_calls"], 2)
        packet = json.loads(client.requests[1][0][-1]["content"])
        self.assertEqual(packet["data"]["pooled"]["priority"]["urgent"]["wait_ms"]["p95"], 10)
        payload = json.dumps(client.requests, ensure_ascii=False)
        self.assertNotIn("MUST_NOT_BE_SENT_TO_MODEL", payload)
        self.assertNotIn(str(self.root), payload)
        self.assertEqual({t["function"]["name"] for t in client.requests[0][1]}, {"query_status", "read_report"})

    def test_model_cannot_choose_paths_or_shell_tools(self):
        secret = self.root / "secret.json"
        secret.write_text('{"secret":"DO_NOT_READ"}', encoding="utf-8")
        for invalid in (tool("run_shell"), tool(args=json.dumps({"path": str(secret)})), tool(args="not json")):
            with self.subTest(invalid=invalid):
                client = FakeClient([invalid, tool(ident="good"), answer()])
                result = self.agent.ask("查询报告", client)
                self.assertEqual(result["mode"], "llm")
                self.assertEqual(result["tool_trace"][0]["tool"], "rejected_tool")
                self.assertNotIn("DO_NOT_READ", json.dumps(client.requests))
        self.assertTrue(secret.exists())

    def test_text_without_reading_evidence_requires_an_actual_tool(self):
        client = FakeClient([answer("我猜延迟提高了"), tool(), answer()])
        result = self.agent.ask("解释数据", client)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["mode"], "llm")
        self.assertNotIn("我猜", result["answer"])

    def test_deep_arguments_are_rejected_without_crashing(self):
        deep = "[" * 70000 + "]" * 70000
        client = FakeClient([tool(args=deep), tool(ident="valid"), answer()])
        result = self.agent.ask("解释数据", client)
        self.assertEqual(result["mode"], "llm")
        self.assertEqual(result["tool_trace"][0]["tool"], "rejected_tool")

    def test_response_without_evidence_hits_budget_and_discards_guess(self):
        client = FakeClient([answer("凭空编造的结论"), answer("凭空编造的结论")])
        result = self.agent.ask("解释数据", client, max_turns=2)
        self.assertEqual(result["mode"], "fallback")
        self.assertEqual(result["model_calls"], 2)
        self.assertIn("最大", result["reason"])
        self.assertNotIn("凭空编造", result["answer"])

    def test_repeated_tool_stops_loop(self):
        client = FakeClient([tool(), tool(ident="call-2")])
        result = self.agent.ask("解释数据", client)
        self.assertEqual(result["mode"], "fallback")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(result["tool_trace"]), 1)
        self.assertIn("重复", result["reason"])

    def test_api_error_falls_back_without_retries(self):
        client = FakeClient([ModelError("model HTTP 403")])
        result = self.agent.ask("解释数据", client)
        self.assertEqual(result["mode"], "fallback")
        self.assertEqual(len(client.requests), 1)
        self.assertIn("100.000", result["evidence_text"])

    def test_tool_call_on_last_turn_is_bounded(self):
        result = self.agent.ask("解释数据", FakeClient([tool()]), max_turns=1)
        self.assertEqual(result["mode"], "fallback")
        self.assertEqual(result["model_calls"], 1)

    def test_both_tools_in_one_response(self):
        request = tool("query_status")
        request["tool_calls"] += tool(ident="report-1")["tool_calls"]
        result = self.agent.ask("解释数据", FakeClient([request, answer()]))
        self.assertEqual(result["mode"], "llm")
        self.assertEqual(len(result["tool_trace"]), 2)

    def test_missing_files_never_produce_model_claims(self):
        self.status.unlink()
        self.report.unlink()
        client = FakeClient([tool(), answer("系统没有问题")])
        result = self.agent.ask("解释数据", client, max_turns=2)
        self.assertEqual(result["mode"], "fallback")
        self.assertIn("无法判断", result["answer"])
        self.assertNotIn("系统没有问题", result["answer"])

    def test_nonfinite_nonobject_and_incomplete_evidence(self):
        for text in ('{"latency":NaN}', '{"latency":1e999}', "[]", '{"pooled":{}}'):
            with self.subTest(text=text):
                self.report.write_text(text, encoding="utf-8")
                self.assertIn("无法判断", self.agent.explain())
        self.report.write_text(json.dumps(fixture_report()), encoding="utf-8")
        data = fixture_report()
        data["pooled"]["fifo"]["urgent"]["completed"] = True
        self.report.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("无法判断", self.agent.explain())

    def test_incomparable_report_does_not_claim_a_strategy_improvement(self):
        data = fixture_report()
        data["comparable_configuration"] = False
        self.report.write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("无法据此比较", self.agent.explain())

    def test_answer_keeps_the_observed_snapshot(self):
        class ChangingClient(FakeClient):
            def complete(inner, messages, tools):
                if inner.requests:
                    self.report.write_text("{}", encoding="utf-8")
                return super().complete(messages, tools)
        result = self.agent.ask("解释数据", ChangingClient([tool(), answer()]))
        self.assertIn("100.000 ms → priority 10.000 ms", result["evidence_text"])

    def test_input_and_turn_limits(self):
        for question in ("", " ", "x" * 2001, None):
            with self.subTest(question=question):
                with self.assertRaises(ValueError):
                    self.agent.ask(question)
        for count in (0, 11, True):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    self.agent.ask("hello", max_turns=count)

    def test_cli_offline_json(self):
        result = subprocess.run([sys.executable, "-Xutf8", "-m", "tools.agent", "ask", "解释调度结果",
            "--status", str(self.status), "--report", str(self.report), "--json"],
            capture_output=True, encoding="utf-8", timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout)["mode"], "rules")


if __name__ == "__main__":
    unittest.main()
