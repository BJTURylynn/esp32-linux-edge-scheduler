"""Small, bounded OpenAI-compatible Chat Completions transport.

No provider, model, or credential is selected implicitly. The caller opts in by
setting EDGE_AGENT_MODEL, EDGE_AGENT_BASE_URL, and EDGE_AGENT_API_KEY. Compatible
providers must support tools, tool_choice, parallel_tool_calls, and max_tokens.
"""
import json
import math
import os
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 256 * 1024


class ModelError(RuntimeError):
    """A model configuration, request, network, or response failure."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None makes urllib raise HTTPError without following Location.
        # In particular, never forward Authorization to another destination.
        return None


def _endpoint(base_url):
    try:
        if (not isinstance(base_url, str) or not base_url
                or base_url != base_url.strip()
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                       for char in base_url)
                or any(char in base_url for char in ("?", "#", "\\"))):
            raise ValueError
        parts = urlsplit(base_url)
        # Accessing port also rejects malformed and out-of-range ports.
        _ = parts.port
        if (parts.scheme not in ("https", "http") or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError
        if parts.scheme == "http" and parts.hostname.lower() not in (
                "localhost", "127.0.0.1", "::1"):
            raise ValueError
        return urlunsplit((parts.scheme, parts.netloc,
                           parts.path.rstrip("/") + "/chat/completions", "", ""))
    except (ValueError, TypeError):
        raise ModelError("Invalid model API base URL; use HTTPS or local loopback HTTP, without credentials, query, or fragment.") from None


def _assistant_message(payload):
    """Validate the wire shape; the agent separately validates tool arguments."""
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or not choices:
            raise ValueError
        message = choices[0]["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        calls = message.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list):
                raise ValueError
            seen = set()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ValueError
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in seen:
                    raise ValueError
                seen.add(call_id)
                function = call.get("function")
                if (not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not function["name"]
                        or not isinstance(function.get("arguments"), str)):
                    raise ValueError
        refusal = message.get("refusal")
        if refusal is not None and not isinstance(refusal, str):
            raise ValueError
        if content is None and not calls and not refusal:
            raise ValueError
        # Deprecated function_call is outside this client's tool-call protocol.
        if message.get("function_call") is not None:
            raise ValueError
        return message
    except (KeyError, TypeError, IndexError, ValueError):
        raise ModelError("Model API returned an invalid assistant message.") from None


class OpenAICompatibleClient:
    def __init__(self, *, model, base_url, api_key, timeout=20.0, opener=None):
        if not isinstance(model, str) or not model.strip():
            raise ModelError("A model must be explicitly configured.")
        if (not isinstance(api_key, str) or not api_key.strip()
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                       for char in api_key)):
            raise ModelError("A valid model API key must be configured in the environment.")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0.1 <= timeout <= 120.0):
            raise ModelError("Model timeout must be between 0.1 and 120 seconds.")
        self.model = model
        self.timeout = float(timeout)
        self._url = _endpoint(base_url)
        self._api_key = api_key
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    @classmethod
    def from_env(cls, *, model=None, base_url=None, timeout=20.0):
        selected_model = os.environ.get("EDGE_AGENT_MODEL") if model is None else model
        selected_url = os.environ.get("EDGE_AGENT_BASE_URL") if base_url is None else base_url
        api_key = os.environ.get("EDGE_AGENT_API_KEY")
        if not selected_model or not selected_url or not api_key:
            raise ModelError("Configure EDGE_AGENT_MODEL, EDGE_AGENT_BASE_URL, and EDGE_AGENT_API_KEY before using the model.")
        return cls(model=selected_model, base_url=selected_url,
                   api_key=api_key, timeout=timeout)

    def complete(self, messages, tools):
        if not isinstance(messages, list) or not messages or not isinstance(tools, list):
            raise ModelError("Model messages and tools must be valid lists.")
        try:
            body = json.dumps({
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "max_tokens": 800,
            }, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request = Request(self._url, data=body, method="POST", headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ModelError("Could not encode the model request.") from None
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.getcode()
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise ModelError("Model API returned an unsuccessful HTTP status.")
                data = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Never include the exception text: it may contain URLs or a body.
            status = exc.code if isinstance(exc.code, int) else "error"
            raise ModelError(f"Model API HTTP error ({status}).") from None
        except (URLError, OSError, HTTPException, ValueError, UnicodeError):
            raise ModelError("Model API network request failed.") from None
        if not isinstance(data, bytes) or len(data) > MAX_RESPONSE_BYTES:
            raise ModelError("Model API response exceeds the 256 KiB limit or is invalid.")
        try:
            # Strict UTF-8 and constants reject non-JSON NaN/Infinity extensions.
            def reject_constant(value):
                raise ValueError
            payload = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise ModelError("Model API returned invalid JSON.") from None
        return _assistant_message(payload)
