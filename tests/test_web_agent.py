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
            "queue_capacity": 64, "accepted": 40, "completed": 40, "rejected": 0,
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

    def request(self, method, path, value=None, *, headers=None, raw=None):
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
        self.assertEqual(result["evidence"]["label"], "历史实测快照")
        self.assertEqual(result["evidence"]["status"]["data"]["completed"], 40)
        text = json.dumps(result)
        for hidden in (SECRET, "MUST_NOT_LEAVE_SERVER", str(self.root), "Authorization"):
            self.assertNotIn(hidden, text)

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
