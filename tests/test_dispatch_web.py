"""Real loopback HTTP boundaries for explicit human task submission, no LLM."""
import http.client
import json
from pathlib import Path
import threading
import unittest

from tools.agent import TOOLS
from tools.dispatch_client import DispatchError, batch_parameters
from tools.web_agent import WebApp, make_server

RUN = "0123456789abcdef"


class Central:
    def __init__(self, **kwargs):
        self.calls = []
        self.unavailable = False

    def state(self):
        if self.unavailable:
            raise DispatchError("private endpoint detail")
        return {"ok": True, "run_id": RUN, "policy": "priority",
                "queue_depth": 0, "queue_capacity": 64,
                "counts": {"succeeded": 0}, "nodes": [], "tasks": []}

    def submit(self, body):
        values = batch_parameters(body)
        self.calls.append(("submit", values))
        return {"ok": True, "accepted": [{"index": 0, "task_id": 1}],
                "rejected": [{"index": 1, "error": "queue_full"}]}

    def retry(self, task_id, run_id=None):
        self.calls.append(("retry", task_id, run_id))
        return {"ok": True, "task_id": 2}


class DispatchWebTests(unittest.TestCase):
    def setUp(self):
        self.app = WebApp(Path("missing-status"), Path("missing-report"),
                          dispatch_client_factory=Central)
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method, path, body=None, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Edge-CSRF"] = self.app.csrf_token
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, None if body is None else json.dumps(body).encode(), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_live_state_is_separate_from_historical_sources(self):
        status, result = self.request("GET", "/api/dispatch/state")
        self.assertEqual(status, 200)
        self.assertTrue(result["available"])
        self.assertEqual(result["csrf_token"], self.app.csrf_token)
        self.assertEqual(result["run_id"], RUN)
        self.assertNotIn("evidence", result)
        self.assertEqual(self.request("GET", "/api/dispatch/state?host=elsewhere")[0], 400)

    def test_unavailable_central_never_returns_historical_success(self):
        self.app.dispatch_client.unavailable = True
        status, result = self.request("GET", "/api/dispatch/state")
        self.assertEqual(status, 503)
        self.assertFalse(result["available"])
        self.assertNotIn("tasks", result)
        self.assertNotIn("private endpoint", str(result))

    def test_submission_receipt_keeps_partial_rejections_explicit(self):
        status, result = self.request("POST", "/api/dispatch/tasks", {"run_id": RUN, "batch_size": 2})
        self.assertEqual(status, 200)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["rejected"][0]["error"], "queue_full")
        self.assertEqual(self.app.dispatch_client.calls[0][0], "submit")

    def test_submit_requires_csrf_and_epoch_and_rejects_endpoint_or_path(self):
        self.assertEqual(self.request("POST", "/api/dispatch/tasks", {"run_id": RUN}, token=False)[0], 403)
        for body in ({}, {"run_id": "bad"}, {"run_id": RUN, "host": "elsewhere"},
                     {"run_id": RUN, "path": "private"}, {"run_id": RUN, "passes": True},
                     {"run_id": RUN, "batch_size": 33}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/dispatch/tasks", body)[0], 400)
        self.assertEqual(self.app.dispatch_client.calls, [])

    def test_retry_binds_id_to_displayed_run(self):
        status, result = self.request("POST", "/api/dispatch/tasks/1/retry", {"run_id": RUN})
        self.assertEqual(status, 200)
        self.assertEqual(result["task_id"], 2)
        self.assertEqual(self.app.dispatch_client.calls, [("retry", 1, RUN)])
        self.assertEqual(self.request("POST", "/api/dispatch/tasks/1/retry", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/dispatch/tasks/1/retry", {"run_id": RUN, "source": "example"})[0], 400)
        self.assertEqual(self.request("POST", "/api/dispatch/tasks/no/retry", {"run_id": RUN})[0], 400)

    def test_uncertain_submission_has_receipts_and_is_not_repeated(self):
        calls = []
        def uncertain(body):
            calls.append(body)
            raise DispatchError("private transport message", receipts={"accepted": [{"task_id": 3}],
                "rejected": [], "unknown_index": 1, "not_submitted": [2]})
        self.app.dispatch_client.submit = uncertain
        status, result = self.request("POST", "/api/dispatch/tasks", {"run_id": RUN, "batch_size": 3})
        self.assertEqual(status, 503)
        self.assertEqual(result["receipts"]["unknown_index"], 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private transport", str(result))

    def test_model_tool_capabilities_remain_read_only(self):
        self.assertEqual({tool["function"]["name"] for tool in TOOLS}, {"query_status", "read_report"})


if __name__ == "__main__":
    unittest.main()
