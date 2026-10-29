"""Loopback HTTP regressions for the local read-only assistant, no real API."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from tools.llm import ModelError
from tools.web_agent import WebApp, make_server


SECRET = "local-test-secret"


class DemoClient:
    def __init__(self, **kwargs):
        self.calls = 0
        self.last_response_metadata = {}

    def complete(self, messages, tools):
        self.calls += 1
        self.last_response_metadata = {
            "status": 200, "elapsed_seconds": .01, "id": f"call-{self.calls}",
            "model": "demo-model", "finish_reason": "tool_calls" if self.calls == 1 else "stop",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
        if self.calls == 1:
            return {"role": "assistant", "content": None,
                    "reasoning_content": "PRIVATE_THOUGHT",
                    "tool_calls": [{"id": "report-call", "type": "function",
                                    "function": {"name": "read_report", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "紧急等待下降，普通等待略有增加；结论仅限本次合成负载。"}


class FailureClient(DemoClient):
    def complete(self, messages, tools):
        raise ModelError("Model API HTTP error (403).")


class WebAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / "status.json"
        self.report = self.root / "report.json"
        self.status.write_text(json.dumps({"policy": "priority", "queue_depth": 0,
            "queue_capacity": 64, "accepted": 7, "completed": 7, "rejected": 0,
            "window": 8, "passes": 1, "running": False, "active": False,
            "accepting": False, "fatal": False}), encoding="utf-8")
        def metric(value):
            return {"completed": 4, "rejected": 0,
                    "wait_ms": {"mean": value, "p95": value, "max": value},
                    "total_ms": {"mean": value + 1, "p95": value + 1, "max": value + 1}}
        self.report.write_text(json.dumps({"schema_version": 1,
            "data_source": "synthetic", "comparable_configuration": True,
            "p95_method": "nearest_rank", "pooled": {
                "fifo": {"urgent": metric(100), "normal": metric(50)},
                "priority": {"urgent": metric(10), "normal": metric(60)}},
            "metadata": {"private": "MUST_NOT_LEAVE_SERVER"}}), encoding="utf-8")
        self.app = WebApp(self.status, self.report, api_key=SECRET, client_factory=DemoClient)
        self.server = make_server(self.app, port=0)
        self.host, self.port = self.server.server_address[:2]
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(3)

    def request(self, method, path, value=None, *, headers=None, raw=None, source="local"):
        # Existing fixture-based tests explicitly use local evidence. Passing
        # source=None exercises the public API's example default unchanged.
        if method == "GET" and path == "/api/state" and source is not None:
            path += "?source=" + source
        if method == "POST" and path == "/api/chat" and isinstance(value, dict) and source is not None:
            value = {"source": source, **value}
        selected = {"Content-Type": "application/json", "X-Edge-CSRF": self.app.csrf_token}
        selected.update(headers or {})
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=selected)
            response = connection.getresponse()
            data = response.read()
            result = json.loads(data) if "application/json" in response.getheader("Content-Type", "") else data.decode()
            return response.status, result, dict(response.getheaders())
        finally:
            connection.close()

    def test_state_exposes_filtered_historical_evidence_not_key_or_paths(self):
        status, result, headers = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(result["key_configured"])
        self.assertEqual(result["csrf_token"], self.app.csrf_token)
        self.assertEqual(result["model"], self.app.model)
        self.assertEqual(result["evidence"]["kind"], "snapshot")
        self.assertEqual(result["source"], "local")
        self.assertEqual(result["evidence"]["source"], "local")
        self.assertEqual(result["evidence"]["label"], "本机实验 · 文件快照")
        self.assertFalse(result["evidence"]["empty"])
        self.assertFalse(result["evidence"]["partial"])
        self.assertEqual(result["evidence"]["status"]["data"]["completed"], 7)
        text = json.dumps(result)
        for hidden in (SECRET, "MUST_NOT_LEAVE_SERVER", str(self.root), "Authorization"):
            self.assertNotIn(hidden, text)

    def test_default_source_is_explicitly_bundled_example_not_local(self):
        status, result, _ = self.request("GET", "/api/state", source=None)
        self.assertEqual(status, 200)
        self.assertEqual(result["source"], "example")
        self.assertEqual(result["evidence"]["source"], "example")
        self.assertEqual(result["evidence"]["label"], "示例数据 · 作者历史结果")
        self.assertEqual(result["evidence"]["status"]["data"]["completed"], 40)
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "rules"}, source=None)
        self.assertEqual(status, 200)
        self.assertEqual(result["source"], "example")
        self.assertEqual(result["source_label"], "示例数据 · 作者历史结果")
        self.assertIn("170.845", result["answer"])

    def test_local_without_records_never_falls_back_to_example_or_calls_model(self):
        self.status.unlink()
        self.report.unlink()
        constructed = []
        def forbidden_client(**kwargs):
            constructed.append(True)
            raise AssertionError("No-evidence requests must not construct a model client")
        self.app.client_factory = forbidden_client
        status, result, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(result["source"], "local")
        self.assertTrue(result["evidence"]["empty"])
        self.assertFalse(result["evidence"]["partial"])
        for kind in ("status", "report"):
            self.assertFalse(result["evidence"][kind]["available"])
            self.assertNotIn("data", result["evidence"][kind])
        for mode in ("llm", "rules"):
            status, result, _ = self.request("POST", "/api/chat", {"question": "完成了吗", "mode": mode})
            self.assertEqual(status, 409)
            self.assertIn("暂无实验记录", result["error"])
        self.assertEqual(constructed, [])
        self.assertEqual(self.request("GET", "/api/state", source="example")[1]["evidence"]["status"]["data"]["completed"], 40)

    def test_partial_local_status_does_not_borrow_example_report(self):
        self.report.unlink()
        status, result, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        evidence = result["evidence"]
        self.assertFalse(evidence["empty"])
        self.assertTrue(evidence["partial"])
        self.assertEqual(evidence["status"]["data"]["completed"], 7)
        self.assertFalse(evidence["report"]["available"])
        self.assertNotIn("data", evidence["report"])
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "rules"})
        self.assertEqual(status, 200)
        self.assertEqual(result["source"], "local")
        self.assertNotIn("170.845", result["answer"])
        self.assertIn("没有可读取的实验报告", result["answer"])

    def test_invalid_local_status_stays_unavailable_with_local_report_only(self):
        self.status.write_text("not JSON", encoding="utf-8")
        _, result, _ = self.request("GET", "/api/state")
        evidence = result["evidence"]
        self.assertTrue(evidence["partial"])
        self.assertFalse(evidence["empty"])
        self.assertFalse(evidence["status"]["available"])
        self.assertNotIn("data", evidence["status"])
        self.assertEqual(evidence["report"]["data"]["pooled"]["fifo"]["urgent"]["wait_ms"]["p95"], 100)

    def test_source_requests_are_isolated_when_interleaved(self):
        for source, completed in (("example", 40), ("local", 7), ("example", 40), ("local", 7)):
            status, result, _ = self.request("GET", "/api/state", source=source)
            self.assertEqual(status, 200)
            self.assertEqual(result["source"], source)
            self.assertEqual(result["evidence"]["status"]["data"]["completed"], completed)

    def test_invalid_and_duplicate_sources_are_rejected(self):
        for query in ("source=unknown", "source=", "source=example&source=local", "source=local&source=local"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/state?" + query, source=None)[0], 400)
        for source in ("unknown", "", None, 123, ["local"], {"source": "local"}):
            with self.subTest(source=source):
                self.assertEqual(self.request("POST", "/api/chat", {"question": "解释报告", "source": source, "mode": "rules"}, source=None)[0], 400)

    def test_model_receives_only_selected_local_tool_evidence(self):
        tool_packets = []
        class InspectingClient(DemoClient):
            def complete(self, messages, tools):
                for message in messages:
                    if message["role"] == "tool":
                        tool_packets.append(json.loads(message["content"]))
                return super().complete(messages, tools)
        self.app.client_factory = InspectingClient
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "llm"})
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["source"], "local")
        self.assertEqual(result["source_label"], "本机实验 · 文件快照")
        self.assertEqual(len(tool_packets), 1)
        self.assertEqual(tool_packets[0]["data"]["pooled"]["fifo"]["urgent"]["wait_ms"]["p95"], 100)
        self.assertNotIn("170.845", json.dumps(tool_packets))

    def test_health_is_local_service_health_only(self):
        status, result, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

    def test_config_and_clear_are_in_memory_and_do_not_echo_the_key(self):
        for invalid in ("", "contains space", "x" * 513, 123):
            with self.subTest(invalid_key_type=type(invalid).__name__):
                status, _, _ = self.request("POST", "/api/config", {"api_key": invalid})
                self.assertEqual(status, 400)
        status, result, _ = self.request("POST", "/api/config", {"api_key": "another-test-key"})
        self.assertEqual(status, 200)
        self.assertTrue(result["key_configured"])
        self.assertNotIn("another-test-key", json.dumps(result))
        status, result, _ = self.request("POST", "/api/config", {"action": "clear"})
        self.assertEqual(status, 200)
        self.assertFalse(result["key_configured"])
        self.assertEqual(self.request("GET", "/api/state")[1]["key_configured"], False)

    def test_rules_work_without_any_api_key(self):
        self.request("POST", "/api/config", {"action": "clear"})
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "rules"})
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "rules")
        self.assertEqual(result["model_calls"], 0)
        self.assertIn("100.000", result["answer"])

    def test_llm_without_key_is_explicit_error(self):
        self.request("POST", "/api/config", {"action": "clear"})
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "llm"})
        self.assertEqual(status, 400)
        self.assertIn("error", result)

    def test_real_fake_tool_loop_returns_trace_and_no_reasoning_or_secret(self):
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "llm"})
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertEqual(result["mode"], "llm")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tool_trace"], [{"tool": "read_report", "available": True}])
        for hidden in (SECRET, "PRIVATE_THOUGHT", "MUST_NOT_LEAVE_SERVER", "Authorization"):
            self.assertNotIn(hidden, json.dumps(result))

    def test_fallback_is_visible_and_not_success(self):
        self.app.client_factory = FailureClient
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告", "mode": "llm"})
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "fallback")
        self.assertFalse(result["success"])
        self.assertIn("403", result["reason"])

    def test_unverified_model_receipt_uses_explicit_rules_fallback(self):
        class UnverifiedClient(DemoClient):
            def complete(self, messages, tools):
                result = super().complete(messages, tools)
                self.last_response_metadata["status"] = 201
                return result
        self.app.client_factory = UnverifiedClient
        status, result, _ = self.request("POST", "/api/chat", {"question": "解释报告"})
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "fallback")
        self.assertFalse(result["success"])
        self.assertIn("校验未通过", result["reason"])
        self.assertIn("100.000", result["answer"])
        self.assertEqual(result["model_calls"], 2)

    def test_csrf_and_untrusted_origin_are_rejected(self):
        for headers in ({"X-Edge-CSRF": "wrong"}, {"X-Edge-CSRF": ""},
                        {"Origin": "https://evil.example"},
                        {"Origin": "http://127.0.0.1:1"},
                        {"Origin": f"https://127.0.0.1:{self.port}"}):
            with self.subTest(headers=headers):
                status, result, _ = self.request("POST", "/api/chat", {"question": "问", "mode": "rules"}, headers=headers)
                self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/chat", {"question": "问", "mode": "rules"}, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_foreign_host_is_rejected_for_get_and_post(self):
        for method, path in (("GET", "/api/state"), ("POST", "/api/config")):
            with self.subTest(method=method):
                status, _, _ = self.request(method, path, {"action": "clear"} if method == "POST" else None,
                                            headers={"Host": f"evil.example:{self.port}"})
                self.assertEqual(status, 403)

    def test_invalid_json_content_type_and_oversized_bodies_are_bounded(self):
        self.assertEqual(self.request("POST", "/api/chat", raw=b"{")[0], 400)
        self.assertEqual(self.request("POST", "/api/chat", raw=b"x", headers={"Content-Type": "text/plain"})[0], 400)
        self.assertEqual(self.request("POST", "/api/chat", raw=b"x" * 16385)[0], 413)

    def test_deep_json_is_a_bounded_error_and_server_remains_usable(self):
        self.assertEqual(self.request("POST", "/api/chat", raw=b"[" * 1500 + b"0" + b"]" * 1500)[0], 400)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_bad_questions_and_arbitrary_paths_or_endpoints_are_rejected(self):
        for body in ({"question": "", "mode": "rules"},
                     {"question": "x" * 2001, "mode": "rules"},
                     {"question": "问", "mode": "shell"},
                     {"question": "问", "mode": "rules", "path": str(self.report)},
                     {"question": "问", "mode": "llm", "base_url": "https://evil.example"}):
            with self.subTest(fields=list(body)):
                self.assertEqual(self.request("POST", "/api/chat", body)[0], 400)
        self.assertEqual(self.request("POST", "/api/config", {"api_key": "valid-key", "model": "other-model"})[0], 400)

    def test_static_routes_do_not_expose_arbitrary_files_or_credentials(self):
        for route in ("/", "/app.js", "/styles.css"):
            with self.subTest(route=route):
                status, body, _ = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertNotIn(SECRET, body)
        for route in ("/../tools/web_agent.py", "/tools/web_agent.py", "/api/unknown"):
            self.assertEqual(self.request("GET", route)[0], 404)

    def test_parallel_model_requests_do_not_duplicate_api_calls(self):
        entered, release = threading.Event(), threading.Event()
        class BlockingClient(DemoClient):
            def complete(self, messages, tools):
                if self.calls == 0:
                    entered.set()
                    if not release.wait(4):
                        raise ModelError("test timed out")
                return super().complete(messages, tools)
        self.app.client_factory = BlockingClient
        first = []
        thread = threading.Thread(target=lambda: first.append(self.request("POST", "/api/chat", {"question": "报告", "mode": "llm"})), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request("POST", "/api/chat", {"question": "重复", "mode": "llm"})[0], 409)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first[0][0], 200)
        self.assertTrue(first[0][1]["success"])


if __name__ == "__main__":
    unittest.main()
