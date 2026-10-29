"""Explicit AIHubMix live demo: prompt for a key, call tools, save redacted proof.

The key comes from EDGE_AGENT_API_KEY or a hidden prompt; it is never persisted.
This opt-in command uses the selected free model. The normal agent stays offline.
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path

from .agent import ReadOnlyAgent
from .llm import ModelError, OpenAICompatibleClient


MODEL = "coding-glm-5.3-flash-free"
BASE_URL = "https://aihubmix.com/v1"
ROOT = Path(__file__).resolve().parents[1]
QUESTION = ("请先读取实验报告和状态，用200字以内解释两种调度策略的等待差异，"
            "说明是否能证明吞吐量提高；状态只代表历史快照。")


def _redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _digest(path):
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class RecordingClient:
    def __init__(self, client):
        self.client = client
        self.trace = []

    def complete(self, messages, tools):
        try:
            return self.client.complete(messages, tools)
        finally:
            # Preserve a received length/content_filter diagnostic even when
            # transport validation rejects that response. HTTP failures may
            # have no safe response metadata and remain in agent.reason.
            metadata = self.client.last_response_metadata
            if metadata:
                self.trace.append(dict(metadata))


def run_live(*, question, api_key, status, report, output, model=MODEL,
             base_url=BASE_URL, timeout=60, max_turns=5, max_output_tokens=1600,
             client_factory=OpenAICompatibleClient):
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("question must contain 1..2000 characters")
    if type(max_turns) is not int or not 1 <= max_turns <= 10:
        raise ValueError("max_turns must be 1..10")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    status, report = Path(status), Path(report)
    hashes = {"status": _digest(status), "report": _digest(report)}
    client = RecordingClient(client_factory(model=model, base_url=base_url,
                             api_key=api_key, timeout=timeout,
                             max_output_tokens=max_output_tokens))
    result = ReadOnlyAgent(status, report).ask(question, client, max_turns=max_turns)
    calls = result["model_calls"]
    successful_tools = [item for item in result["tool_trace"]
                        if item.get("tool") in ("query_status", "read_report")
                        and item.get("available") is True]
    success = (result["mode"] == "llm" and result["reason"] is None
               and calls >= 2 and bool(successful_tools) and bool(result["answer"].strip())
               and len(client.trace) == calls
               and all(item.get("status") == 200
                       and item.get("finish_reason") not in ("length", "content_filter")
                       for item in client.trace))
    payload = _redact({"success": success, "requested_model": model,
                      "base_url": base_url, "question": question,
                      "api_trace": client.trace, "agent": result,
                      "evidence_sha256": hashes, "key_saved": False,
                      "scope": "Historical synthetic experiment evidence; read-only explanation, no scheduling control."}, api_key)
    (output / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                                       + "\n", encoding="utf-8")
    (output / "answer.md").write_text(payload["agent"]["answer"] + "\n", encoding="utf-8")
    text = ("# 大模型只读工具调用实测\n\n"
            f"验收：{'通过' if success else '未通过'}\n\n"
            f"- 请求模型：{payload['requested_model']}\n"
            f"- 返回模式：{result['mode']}\n"
            f"- 模型请求次数：{calls}\n"
            f"- 成功工具：{', '.join(item['tool'] for item in successful_tools) or '无'}\n"
            f"- 回退原因：{result['reason'] or '无'}\n\n"
            "API白名单元数据与证据哈希见result.json，模型解释见answer.md。"
            "不保存密钥、HTTP授权头或思考文本。"
            "模型解释需核对程序证据，不能据此证明进程在线、吞吐提高或硬实时。\n")
    (output / "report.md").write_text(text, encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?", default=QUESTION)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--status", type=Path, default=ROOT / "docs/evidence/project-demo/service/status.json")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/evidence/dual-device-scheduler/comparison/report.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--max-output-tokens", type=int, default=1600)
    args = vars(parser.parse_args())
    if args["output"].exists():
        parser.error("output exists; use a new directory to preserve prior evidence")
    key = os.environ.get("EDGE_AGENT_API_KEY")
    if key is None:
        key = getpass.getpass("Model API key (hidden): ")
    try:
        result = run_live(api_key=key, **args)
    except (OSError, ValueError, ModelError) as exc:
        parser.error(str(exc))
    print(json.dumps({"success": result["success"], "mode": result["agent"]["mode"],
                      "model_calls": result["agent"]["model_calls"],
                      "tool_trace": result["agent"]["tool_trace"],
                      "reason": result["agent"]["reason"], "output": str(args["output"])},
                     ensure_ascii=False, indent=2))
    print("\n" + result["agent"]["answer"])
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
