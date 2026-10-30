"""Local-only web workbench for the existing read-only operations assistant.

Model tools remain read-only. Explicit human task submission uses a separate
fixed central-service client. Bind to loopback; model keys stay in memory.
"""
import argparse
from contextlib import nullcontext
import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .agent import ReadOnlyAgent
from .agent_live import BASE_URL, MODEL, ROOT, RecordingClient, _redact
from .llm import ModelError, OpenAICompatibleClient
from .dispatch_client import DispatchClient, DispatchError
from .dispatch_wifi import WiFiError


MAX_BODY = 16384
EXAMPLE_STATUS = ROOT / "docs/evidence/project-demo/service/status.json"
EXAMPLE_REPORT = ROOT / "docs/evidence/dual-device-scheduler/comparison/report.json"
SOURCES = {
    "example": ("示例数据 · 作者历史结果", "来自项目作者已保存的实验记录，仅供演示，不代表本机已运行实验或设备当前在线。"),
    "local": ("本机实验 · 文件快照", "只读取启动时配置的本机结果文件；刷新不会运行实验，文件快照不代表设备或服务当前在线。"),
}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/dispatch.js": ("dispatch.js", "text/javascript; charset=utf-8"),
          "/dispatch.css": ("dispatch.css", "text/css; charset=utf-8"),
          "/wifi.js": ("wifi.js", "text/javascript; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}


class ApiError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class SnapshotAgent(ReadOnlyAgent):
    """Keep preflight and model tools on the same source observations."""
    def __init__(self, status_path, report_path, source):
        super().__init__(status_path, report_path)
        self._snapshot = {}
        self._origin = SOURCES[source][0]

    def _evidence(self, tool):
        if tool not in self._snapshot:
            result = super()._evidence(tool)
            if result["available"]:
                result["data"]["evidence_origin"] = self._origin
            self._snapshot[tool] = result
        return self._snapshot[tool]


class WebApp:
    def __init__(self, status_path, report_path, api_key=None, model=MODEL,
                 base_url=BASE_URL, client_factory=OpenAICompatibleClient,
                 dispatch_host="127.0.0.1", dispatch_port=9100,
                 dispatch_client_factory=DispatchClient, wifi_controller=None):
        self.status_path = Path(status_path)
        self.report_path = Path(report_path)
        self.model, self.base_url = model, base_url
        self.client_factory = client_factory
        self.dispatch_client = dispatch_client_factory(host=dispatch_host, port=dispatch_port)
        self.wifi_controller = wifi_controller
        self.csrf_token = secrets.token_urlsafe(32)
        self._key = None
        self._key_lock = threading.Lock()
        self._chat_lock = threading.Lock()
        # Validate the operator's endpoint and model without any network call.
        self._validate_key(api_key or "not-configured")
        if api_key:
            self.set_key(api_key)

    def _validate_key(self, key):
        if (not isinstance(key, str) or not key or len(key) > 512
                or not all(33 <= ord(char) <= 126 for char in key)):
            raise ApiError(400, "请输入有效的模型密钥。")
        OpenAICompatibleClient(model=self.model, base_url=self.base_url,
                               api_key=key, timeout=60, max_output_tokens=1600)

    @property
    def key_configured(self):
        with self._key_lock:
            return self._key is not None

    def set_key(self, key):
        self._validate_key(key)
        with self._key_lock:
            self._key = key

    def configure(self, body):
        if set(body) == {"action"} and body["action"] == "clear":
            with self._key_lock:
                self._key = None
        elif set(body) == {"api_key"}:
            self.set_key(body["api_key"])
        else:
            raise ApiError(400, "配置仅接受模型密钥或清除操作。")
        return {"key_configured": self.key_configured, "model": self.model}

    def dispatch_state(self):
        result = self.dispatch_client.state()
        return {**result, "available": True, "csrf_token": self.csrf_token}

    def wifi_state(self):
        # A missing central handshake cannot establish whether Wi-Fi itself is
        # disconnected. USB observations expire; never reuse history as live.
        base = (self.wifi_controller.snapshot() if self.wifi_controller else
                {"supported": False, "busy": False, "phase": "idle", "host": None,
                 "serial_port": None, "observation": {},
                 "message": "请通过 tools.dispatch_start 统一启动器启用网页配网。"})
        node, central_available = {}, False
        try:
            central = self.dispatch_client.state()
            central_available = True
            node = next((n for n in central.get("nodes", [])
                         if n.get("device") == "esp32c3-01"), {})
        except DispatchError:
            pass
        online = node.get("state") in ("idle", "running", "dispatching")
        observation = base.get("observation") or {}
        observed_at = observation.get("observed_at")
        fresh = (type(observed_at) in (int, float)
                 and 0 <= time.time() - observed_at <= 15)
        wifi_state, source = "unknown", "unknown"
        if online:
            wifi_state, source = "connected", "central_tcp"
        elif base.get("phase") == "connecting":
            wifi_state = "connecting"
        elif fresh and observation.get("state") in ("connected", "disconnected"):
            wifi_state, source = observation["state"], observation.get("source", "usb")
        return {**base, "central_available": central_available,
                "central_state": node.get("state", "offline") if central_available else "unknown",
                "wifi_state": wifi_state, "wifi_source": source, "csrf_token": self.csrf_token}

    def wifi_ports(self):
        if not self.wifi_controller:
            return {"ports": [], "serial_available": False, "supported": False}
        return self.wifi_controller.ports()

    def wifi_operation(self, operation, body):
        if not self.wifi_controller:
            raise ApiError(409, "当前启动方式未启用配网，请使用 tools.dispatch_start。")
        return getattr(self.wifi_controller, operation)(body)

    def task_guard(self):
        return self.wifi_controller.task_guard() if self.wifi_controller else nullcontext()

    @staticmethod
    def dispatch_run(body):
        run = body.get("run_id")
        if not isinstance(run, str) or not re.fullmatch(r"[0-9a-f]{16}", run):
            raise ApiError(400, "请刷新中央状态后再操作，任务必须绑定当前运行编号。")
        return run

    def _source_paths(self, source):
        if not isinstance(source, str) or source not in SOURCES:
            raise ApiError(400, "请选择示例数据或本机实验。")
        return ((EXAMPLE_STATUS, EXAMPLE_REPORT) if source == "example"
                else (self.status_path, self.report_path))

    def state(self, source="example"):
        status_path, report_path = self._source_paths(source)
        agent = ReadOnlyAgent(status_path, report_path)
        evidence = {"kind": "snapshot", "source": source,
                    "label": SOURCES[source][0], "description": SOURCES[source][1]}
        for field, tool, path in (("status", "query_status", status_path),
                                  ("report", "read_report", report_path)):
            value = agent._evidence(tool)
            record = {"available": value["available"]}
            if value["available"]:
                record["data"] = value["data"]
                try:
                    record["file_updated_epoch"] = path.stat().st_mtime
                except OSError:
                    pass
            else:
                record["reason"] = "本机记录尚未生成或不可读取。" if source == "local" else "示例记录暂不可读取。"
            evidence[field] = record
        available = sum(evidence[field]["available"] for field in ("status", "report"))
        evidence.update(empty=available == 0, partial=available == 1)
        return {"model": self.model, "key_configured": self.key_configured,
                "source": source, "csrf_token": self.csrf_token, "evidence": evidence}

    def chat(self, body):
        if not {"question"} <= set(body) <= {"question", "mode", "source"}:
            raise ApiError(400, "提问仅接受question、mode和source。")
        question, mode = body["question"], body.get("mode", "llm")
        source = body.get("source", "example")
        status_path, report_path = self._source_paths(source)
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ApiError(400, "请输入1至2000字的问题。")
        if mode not in ("llm", "rules"):
            raise ApiError(400, "请选择大模型或离线规则模式。")
        agent = SnapshotAgent(status_path, report_path, source)
        if not any([agent._evidence(tool)["available"]
                    for tool in ("query_status", "read_report")]):
            raise ApiError(409, "暂无实验记录，请先运行本机实验并配置结果文件。" if source == "local"
                           else "示例记录暂不可读取，请检查项目文件。")
        with self._key_lock:
            key = self._key
        if mode == "llm" and not key:
            raise ApiError(400, "请先配置模型密钥，或切换到离线规则。")
        if not self._chat_lock.acquire(blocking=False):
            raise ApiError(409, "上一条分析尚未结束，请稍后再试。")
        started = time.monotonic()
        try:
            client = None
            if mode == "llm":
                client = RecordingClient(self.client_factory(model=self.model,
                    base_url=self.base_url, api_key=key, timeout=60,
                    max_output_tokens=1600))
            result = agent.ask(question.strip(), client=client, max_turns=5)
            trace = client.trace if client else []
            allowed = any(item.get("available") is True
                          and item.get("tool") in ("query_status", "read_report")
                          for item in result["tool_trace"])
            valid_llm = (result["mode"] == "llm" and result["reason"] is None
                         and result["model_calls"] >= 2 and allowed
                         and len(trace) == result["model_calls"]
                         and all(item.get("status") == 200
                                 and item.get("finish_reason") not in ("length", "content_filter")
                                 for item in trace))
            if mode == "llm" and result["mode"] == "llm" and not valid_llm:
                # A model answer without a complete, verified API/tool receipt
                # must not look like a successful model-backed explanation.
                rules = agent.ask(question.strip())
                result.update(mode="fallback", answer=rules["answer"],
                              evidence_text=rules["evidence_text"],
                              reason="模型响应校验未通过，已使用离线规则解释。")
            # Fixed paths are operator configuration. Strip them from UI text;
            # the model already receives only the filtered numerical packet.
            for field in ("answer", "evidence_text"):
                for path in (status_path, report_path):
                    result[field] = result[field].replace(str(path), "已配置的实验记录")
            result["evidence_text"] = SOURCES[source][0] + "\n" + result["evidence_text"]
            result.update(success=(mode == "rules" and result["mode"] == "rules") or valid_llm,
                          source=source, source_label=SOURCES[source][0],
                          api_trace=trace, elapsed_seconds=round(time.monotonic()-started, 3))
            return _redact(result, key)
        finally:
            self._chat_lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "EdgeScope"

    def log_message(self, format, *args):
        # Do not log request bodies, query strings, credentials or model text.
        pass

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self'; script-src 'self'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _host(self):
        allowed = {f"127.0.0.1:{self.server.server_port}",
                   f"localhost:{self.server.server_port}"}
        value = self.headers.get("Host", "").lower()
        if value not in allowed:
            raise ApiError(403, "仅允许从本机页面访问。")
        return value

    def _post_body(self):
        self._host()
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.server.server_port}",
                   f"http://localhost:{self.server.server_port}"}
        if origin is not None and origin not in allowed:
            raise ApiError(403, "请求来源不受支持。")
        token = self.headers.get("X-Edge-CSRF", "")
        if (not token.isascii() or len(token) > 256
                or not secrets.compare_digest(token, self.server.app.csrf_token)):
            raise ApiError(403, "页面凭证已失效，请刷新后重试。")
        if self.headers.get_content_type() != "application/json":
            raise ApiError(400, "请使用JSON请求。")
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "不支持此请求编码。")
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal():
            raise ApiError(400, "请求长度无效。")
        size = int(length)
        if size > MAX_BODY:
            raise ApiError(413, "请求内容过长。")
        self.connection.settimeout(5)
        try:
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError
            def reject(value):
                raise ValueError
            body = json.loads(data, parse_constant=reject)
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, OSError, RecursionError):
            raise ApiError(400, "请求JSON无效或读取超时。") from None

    def do_GET(self):
        try:
            self._host()
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._send(200, {"ok": True})
            elif path == "/api/dispatch/state":
                if urlsplit(self.path).query:
                    raise ApiError(400, "中央服务地址由启动配置指定。")
                self._send(200, self.server.app.dispatch_state())
            elif path in ("/api/dispatch/wifi", "/api/dispatch/wifi/ports"):
                if urlsplit(self.path).query:
                    raise ApiError(400, "连接参数不能放在网址中。")
                self._send(200, self.server.app.wifi_ports() if path.endswith("/ports")
                           else self.server.app.wifi_state())
            elif path == "/api/state":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if set(query) - {"source"} or len(query.get("source", ["example"])) != 1:
                    raise ApiError(400, "数据入口参数无效。")
                self._send(200, self.server.app.state(query.get("source", ["example"])[0]))
            elif path in STATIC:
                name, content_type = STATIC[path]
                file = ROOT / "web" / name
                if not file.is_file():
                    raise ApiError(404, "页面资源尚未准备好。")
                self._send(200, file.read_bytes(), content_type)
            else:
                raise ApiError(404, "未找到该资源。")
        except ApiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except WiFiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except DispatchError:
            self._send(503, {"available": False, "error": "中央调度服务未连接，请启动第二版派发服务。"})
        except (OSError, ValueError):
            self._send(500, {"error": "本地证据暂不可用，请稍后刷新。"})

    def do_POST(self):
        try:
            body = self._post_body()
            path = urlsplit(self.path).path
            response_status = 200
            if path == "/api/config":
                result = self.server.app.configure(body)
            elif path == "/api/chat":
                result = self.server.app.chat(body)
            elif path == "/api/dispatch/tasks":
                with self.server.app.task_guard():
                    self.server.app.dispatch_run(body)
                    result = self.server.app.dispatch_client.submit(body)
            elif path.startswith("/api/dispatch/tasks/") and path.endswith("/retry"):
                task_id = path[len("/api/dispatch/tasks/"):-len("/retry")]
                if set(body) != {"run_id"} or not task_id.isascii() or not task_id.isdecimal() or not 1 <= len(task_id) <= 10:
                    raise ApiError(400, "重试需要有效任务编号和当前运行编号。")
                with self.server.app.task_guard():
                    result = self.server.app.dispatch_client.retry(int(task_id), run_id=self.server.app.dispatch_run(body))
            elif path in ("/api/dispatch/wifi/connect", "/api/dispatch/wifi/probe", "/api/dispatch/wifi/attach"):
                if urlsplit(self.path).query:
                    raise ApiError(400, "连接参数不能放在网址中。")
                result = self.server.app.wifi_operation(path.rsplit("/", 1)[1], body)
                response_status = 202
            else:
                raise ApiError(404, "未找到该接口。")
            self._send(response_status, result)
        except ApiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except WiFiError as exc:
            self._send(exc.status, {"error": str(exc)})
        except DispatchError as exc:
            result = {"error": "中央服务未确认操作结果，请先刷新任务列表，勿重复提交。"}
            if getattr(exc, "receipts", None):
                result["receipts"] = exc.receipts
            self._send(503, result)
        except ValueError:
            self._send(400, {"error": "任务参数无效，请核对数量、范围及字段。"})
        except ModelError:
            self._send(400, {"error": "模型配置无效，请检查本地配置。"})
        except (OSError, TypeError, KeyError):
            self._send(500, {"error": "本次分析未能完成，请刷新证据后重试。"})


def make_server(app, host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("the assistant serves loopback only")
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--status", type=Path, default=ROOT / "results/live/status.json",
                        help="local experiment status (never used by the example entry)")
    parser.add_argument("--report", type=Path, default=ROOT / "results/comparison/report.json",
                        help="local comparison report (never used by the example entry)")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--prompt-key", action="store_true")
    parser.add_argument("--dispatch-host", default="127.0.0.1")
    parser.add_argument("--dispatch-port", type=int, default=9100)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    key = os.environ.pop("EDGE_AGENT_API_KEY", None)
    if args.prompt_key and not key:
        key = getpass.getpass("Model API key (hidden): ")
    try:
        app = WebApp(args.status, args.report, key, args.model, args.base_url,
                     dispatch_host=args.dispatch_host, dispatch_port=args.dispatch_port)
        del key
        server = make_server(app, port=args.port)
    except (ModelError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"EdgeScope: http://127.0.0.1:{server.server_port}", flush=True)
    print("Separate example/local snapshots; model key stays in server memory.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
