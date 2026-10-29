import io
import json
import socket
import unittest
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, build_opener
from urllib.response import addinfourl

from tools.llm import MAX_RESPONSE_BYTES, ModelError, OpenAICompatibleClient


MESSAGES = [{"role": "user", "content": "只读状态"}]
TOOLS = [{"type": "function", "function": {
    "name": "query_status", "description": "Read status",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}]
ANSWER = {"role": "assistant", "content": "The scheduler is idle."}


def wire(message=ANSWER):
    return json.dumps({"choices": [{"message": message}]}).encode("utf-8")


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status
        self.read_limits = []

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_limits.append(size)
        return super().read(size)


class FakeOpener:
    def __init__(self, data=None, *, error=None, status=200):
        self.response = Response(wire() if data is None else data, status)
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class RedirectingHTTPHandler(HTTPHandler):
    """An in-memory HTTP response; no socket is opened."""
    def __init__(self):
        super().__init__()
        self.requests = []

    def http_open(self, request):
        self.requests.append(request)
        headers = Message()
        headers["Location"] = "https://other.example/steal"
        result = addinfourl(io.BytesIO(b"secret response"), headers,
                            request.full_url, code=302)
        result.msg = "Found"
        return result


class LLMTransportTests(unittest.TestCase):
    def client(self, opener=None, **kwargs):
        settings = {"model": "test-model", "base_url": "https://model.example/v1",
                    "api_key": "test-key", "opener": opener or FakeOpener()}
        settings.update(kwargs)
        return OpenAICompatibleClient(**settings)

    def test_request_body_headers_and_assistant_result(self):
        opener = FakeOpener()
        result = self.client(opener, timeout=4.5).complete(MESSAGES, TOOLS)
        self.assertEqual(result, ANSWER)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://model.example/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(timeout, 4.5)
        self.assertEqual(json.loads(request.data), {
            "model": "test-model", "messages": MESSAGES, "tools": TOOLS,
            "tool_choice": "auto", "parallel_tool_calls": False, "max_tokens": 800,
        })
        self.assertEqual(opener.response.read_limits, [MAX_RESPONSE_BYTES + 1])
        self.assertTrue(opener.response.closed)

    def test_tool_call_shape_is_returned_for_agent_validation(self):
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "query_status", "arguments": "{}"},
        }]}
        self.assertEqual(self.client(FakeOpener(wire(message))).complete(MESSAGES, TOOLS), message)

    def test_environment_is_explicit_and_missing_fields_fail(self):
        environment = {"EDGE_AGENT_MODEL": "env-model",
                       "EDGE_AGENT_BASE_URL": "https://env.example/v1",
                       "EDGE_AGENT_API_KEY": "env-test-key"}
        # clear=True prevents these tests from reading any existing credentials.
        with patch.dict("os.environ", environment, clear=True):
            client = OpenAICompatibleClient.from_env()
            self.assertEqual(client.model, "env-model")
            self.assertEqual(client.timeout, 20.0)
            overridden = OpenAICompatibleClient.from_env(
                model="chosen-model", base_url="http://localhost:8080/v1", timeout=3,
                max_output_tokens=2048)
            self.assertEqual(overridden.model, "chosen-model")
            self.assertEqual(overridden._url, "http://localhost:8080/v1/chat/completions")
            self.assertEqual(overridden.max_output_tokens, 2048)
        for missing in environment:
            incomplete = {key: value for key, value in environment.items() if key != missing}
            with self.subTest(missing=missing), patch.dict("os.environ", incomplete, clear=True):
                with self.assertRaises(ModelError):
                    OpenAICompatibleClient.from_env()
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ModelError):
                OpenAICompatibleClient.from_env(model="model", base_url="https://example.com/v1")

    def test_url_scheme_host_credentials_and_components(self):
        for base in ("https://api.example/v1/", "http://localhost:1234/v1",
                     "http://127.0.0.1/v1", "http://[::1]:1234/v1"):
            with self.subTest(base=base):
                self.assertTrue(self.client(base_url=base)._url.endswith("/v1/chat/completions"))
        invalid = (None, "", "ftp://example.com/v1", "http://api.example/v1",
                   "http://127.0.0.2/v1", "http://localhost.evil/v1", "http://localhost./v1",
                   "https://user:password@example.com/v1", "https://user@example.com/v1",
                   "https://example.com/v1?secret=token", "https://example.com/v1#secret",
                   "https://example.com/v1?", "https://example.com/v1#",
                   "https:///v1", "https://example.com:invalid/v1",
                   "https://example.com:70000/v1", " https://example.com/v1",
                   "https://example.com/\nv1", "https://example.com/space here",
                   "https://example.com\\evil/v1")
        for base in invalid:
            with self.subTest(base=base), self.assertRaises(ModelError):
                self.client(base_url=base)

    def test_timeout_bounds_and_empty_configuration(self):
        for value in (0.1, 120, 20.0):
            self.assertEqual(self.client(timeout=value).timeout, value)
        for value in (0, 0.09, 120.1, True, "20", None, float("nan"), float("inf")):
            with self.subTest(timeout=value), self.assertRaises(ModelError):
                self.client(timeout=value)
        for settings in ({"model": ""}, {"model": None}, {"api_key": ""},
                         {"api_key": "injected\r\nheader"}, {"api_key": " "}):
            with self.subTest(settings=settings), self.assertRaises(ModelError):
                self.client(**settings)

    def test_default_transport_refuses_redirect_without_second_request(self):
        handler = RedirectingHTTPHandler()
        # Reuse the production redirect blocker with an in-memory HTTP handler.
        def make_opener(blocker):
            return build_opener(blocker, handler)
        with patch("tools.llm.build_opener", side_effect=make_opener):
            client = OpenAICompatibleClient(model="test-model", api_key="test-key",
                                            base_url="http://localhost/v1")
            with self.assertRaisesRegex(ModelError, "HTTP error \\(302\\)"):
                client.complete(MESSAGES, TOOLS)
        self.assertEqual(len(handler.requests), 1)
        self.assertEqual(handler.requests[0].host, "localhost")

    def test_bounded_response_allows_exact_limit_and_rejects_extra_byte(self):
        data = wire()
        exact = data + b" " * (MAX_RESPONSE_BYTES - len(data))
        self.assertEqual(self.client(FakeOpener(exact)).complete(MESSAGES, TOOLS), ANSWER)
        opener = FakeOpener(exact + b" ")
        with self.assertRaisesRegex(ModelError, "256 KiB"):
            self.client(opener).complete(MESSAGES, TOOLS)
        self.assertEqual(opener.response.read_limits, [MAX_RESPONSE_BYTES + 1])

    def test_http_network_timeout_and_status_failures_are_not_retried(self):
        errors = [HTTPError("https://private.example/secret-url", 403,
                            "secret-body test-key", {}, io.BytesIO(b"secret-body")),
                  URLError("secret-url test-key"), socket.timeout("test-key"),
                  OSError("test-key"), ValueError("test-key"),
                  IncompleteRead(b"test-key secret-body")]
        for error in errors:
            opener = FakeOpener(error=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(ModelError) as caught:
                self.client(opener).complete(MESSAGES, TOOLS)
            text = str(caught.exception)
            for secret in ("test-key", "secret-body", "secret-url", "private.example"):
                self.assertNotIn(secret, text)
            self.assertEqual(len(opener.requests), 1)
        for status in (302, 403, 500):
            with self.subTest(status=status), self.assertRaises(ModelError):
                self.client(FakeOpener(status=status)).complete(MESSAGES, TOOLS)

    def test_invalid_json_and_encoding_are_sanitized(self):
        for data in (b"not json test-key", b"{", b"\xff", b'{"choices":NaN}',
                     b"[" * 2000):
            with self.subTest(data=data[:30]), self.assertRaisesRegex(ModelError, "invalid JSON") as caught:
                self.client(FakeOpener(data)).complete(MESSAGES, TOOLS)
            self.assertNotIn("test-key", str(caught.exception))

    def test_malformed_choices_and_assistant_messages(self):
        bad_payloads = [None, [], {}, {"choices": []}, {"choices": {}},
                        {"choices": [None]}, {"choices": [{}]},
                        {"choices": [{"message": None}]}]
        bad_messages = [[], {}, {"role": "user", "content": "secret-body"},
                        {"role": "assistant"}, {"role": "assistant", "content": []},
                        {"role": "assistant", "content": None, "tool_calls": {}},
                        {"role": "assistant", "tool_calls": [None]},
                        {"role": "assistant", "refusal": 7},
                        {"role": "assistant", "content": "text", "function_call": {}},
                        {"role": "assistant", "tool_calls": [{"id": "x", "type": "other"}]},
                        {"role": "assistant", "tool_calls": [{"id": "x", "type": "function",
                          "function": {"name": "query_status", "arguments": {}}}]},
                        {"role": "assistant", "tool_calls": [{"id": "", "type": "function",
                          "function": {"name": "query_status", "arguments": "{}"}}]}]
        duplicate = {"id": "same", "type": "function",
                     "function": {"name": "query_status", "arguments": "{}"}}
        bad_messages.append({"role": "assistant", "tool_calls": [duplicate, duplicate]})
        bad_payloads += [{"choices": [{"message": message}]} for message in bad_messages]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(ModelError, "invalid assistant"):
                self.client(FakeOpener(json.dumps(payload).encode())).complete(MESSAGES, TOOLS)

    def test_invalid_input_is_rejected_before_network(self):
        for messages, tools in (([], TOOLS), ({}, TOOLS), (MESSAGES, {}),
                                ([{"content": float("nan")}], TOOLS),
                                ([{"content": object()}], TOOLS)):
            opener = FakeOpener()
            with self.subTest(messages=messages), self.assertRaises(ModelError):
                self.client(opener).complete(messages, tools)
            self.assertEqual(opener.requests, [])

    def test_output_token_budget_bounds_and_request(self):
        for budget in (64, 800, 2048, 4096):
            opener = FakeOpener()
            client = self.client(opener, max_output_tokens=budget)
            self.assertEqual(client.complete(MESSAGES, TOOLS), ANSWER)
            self.assertEqual(json.loads(opener.requests[0][0].data)["max_tokens"], budget)
        for budget in (0, 63, 4097, True, False, 800.0, "800", None,
                       float("nan"), float("inf")):
            opener = FakeOpener()
            with self.subTest(budget=budget), self.assertRaises(ModelError):
                self.client(opener, max_output_tokens=budget)
            self.assertEqual(opener.requests, [])

    def test_truncated_and_filtered_answers_are_rejected(self):
        for reason, expected in (("length", "truncated"), ("content_filter", "content filter")):
            for message in (ANSWER, {"role": "assistant", "content": None}):
                payload = {"id": "completion-1", "model": "test-model", "choices": [
                    {"message": message, "finish_reason": reason}]}
                client = self.client(FakeOpener(json.dumps(payload).encode()))
                with self.subTest(reason=reason, message=message), self.assertRaisesRegex(ModelError, expected):
                    client.complete(MESSAGES, TOOLS)
                self.assertEqual(client.last_response_metadata["finish_reason"], reason)
                self.assertEqual(client.last_response_metadata["status"], 200)

    def test_response_metadata_is_a_detached_whitelist(self):
        payload = {
            "id": "completion-123", "model": "provider-model", "headers": {"Authorization": "test-key"},
            "messages": MESSAGES, "secret": "DO_NOT_SAVE_SECRET",
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19,
                      "completion_tokens_details": {"reasoning_text": "DO_NOT_SAVE_REASONING"}},
            "choices": [{"message": dict(ANSWER, reasoning_content="DO_NOT_SAVE_REASONING"),
                         "finish_reason": "stop", "raw": "DO_NOT_SAVE_RAW"}]}
        client = self.client(FakeOpener(json.dumps(payload).encode()))
        client.complete(MESSAGES, TOOLS)
        metadata = client.last_response_metadata
        self.assertEqual(set(metadata), {"id", "model", "status", "finish_reason", "usage", "elapsed_seconds"})
        self.assertEqual(metadata["usage"], {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19})
        self.assertEqual(metadata["id"], "completion-123")
        self.assertGreaterEqual(metadata["elapsed_seconds"], 0)
        encoded = json.dumps(metadata)
        for excluded in ("test-key", "Authorization", "DO_NOT_SAVE", "messages", "reasoning_content", ANSWER["content"]):
            self.assertNotIn(excluded, encoded)
        metadata["usage"]["total_tokens"] = 999
        self.assertEqual(client.last_response_metadata["usage"]["total_tokens"], 19)
        with self.assertRaises(AttributeError):
            client.last_response_metadata = {}

    def test_metadata_usage_accepts_only_nonnegative_integer_counts(self):
        for invalid in (-1, True, 1.0, "1", None):
            payload = {"choices": [{"message": ANSWER}],
                       "usage": {"prompt_tokens": invalid, "completion_tokens": 0, "total_tokens": 1,
                                 "untrusted": "DO_NOT_SAVE"}}
            client = self.client(FakeOpener(json.dumps(payload).encode()))
            client.complete(MESSAGES, TOOLS)
            self.assertEqual(client.last_response_metadata["usage"], {"completion_tokens": 0, "total_tokens": 1})

    def test_new_call_clears_previous_response_metadata(self):
        for failure in ("invalid_input", "network"):
            opener = FakeOpener()
            client = self.client(opener)
            client.complete(MESSAGES, TOOLS)
            self.assertIn("status", client.last_response_metadata)
            if failure == "network":
                opener.error = OSError("test-key")
                inputs = MESSAGES
            else:
                inputs = []
            with self.assertRaises(ModelError):
                client.complete(inputs, TOOLS)
            self.assertEqual(client.last_response_metadata, {})

    def test_agent_cli_forwards_the_explicit_output_budget(self):
        from contextlib import redirect_stdout
        from tools.agent import main
        result = {"mode": "llm", "reason": None, "answer": "answer", "evidence_text": "evidence"}
        with patch("sys.argv", ["agent", "ask", "解释报告", "--backend", "llm", "--json",
                                "--max-output-tokens", "2048"]), \
             patch("tools.agent.ReadOnlyAgent") as agent, \
             patch("tools.llm.OpenAICompatibleClient.from_env") as client, \
             redirect_stdout(io.StringIO()):
            agent.return_value.ask.return_value = result
            main()
        client.assert_called_once_with(model=None, base_url=None, timeout=20.0,
                                       max_output_tokens=2048)


if __name__ == "__main__":
    unittest.main()
