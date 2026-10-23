"""Read-only operations assistant with exactly two fixed file-reading tools.

No model/API credentials, command execution, server control or arbitrary path tool.
"""
import argparse
import json
from pathlib import Path
from .report import number


class ReadOnlyAgent:
    def __init__(self, status_path, report_path):
        self._paths = {"query_status": Path(status_path), "read_report": Path(report_path)}

    def call(self, tool):
        if tool not in self._paths:
            raise ValueError("unknown tool; allowed: query_status, read_report")
        path = self._paths[tool]
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                return {"available": False, "reason": "file exceeds 8 MiB read limit"}
            with path.open("rb") as handle:
                data = handle.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                return {"available": False, "reason": "file exceeds read limit"}
            return {"available": True, "evidence_file": str(path), "data": json.loads(data)}
        except (OSError, ValueError) as exc:
            return {"available": False, "reason": str(exc)}

    def explain(self):
        evidence = self.call("read_report")
        if not evidence["available"]:
            return "没有可读取的实验报告，无法判断调度是否改善延迟。"
        report = evidence["data"]
        if not isinstance(report, dict) or not report.get("comparable_configuration"):
            return "报告未通过对照配置检查，无法据此比较两种策略。"
        try:
            lines = [f"依据：{evidence['evidence_file']}（合成数据，本地实测）。"]
            for priority, label in (("urgent", "紧急任务"), ("normal", "普通任务")):
                fifo = report["pooled"]["fifo"][priority]
                prio = report["pooled"]["priority"][priority]
                before, after = fifo["wait_ms"]["p95"], prio["wait_ms"]["p95"]
                if before is None or after is None:
                    lines.append(f"{label}缺少完成记录，无法比较 P95。")
                    continue
                change = "降低" if after < before else "升高" if after > before else "不变"
                lines.append(f"{label}等待 P95：FIFO {number(before)} ms → priority {number(after)} ms（{change}）。"
                             f"完成量 {fifo['completed']} / {prio['completed']}，拒绝量 {fifo['rejected']} / {prio['rejected']}。")
            lines += ["机制解释：priority 优先选取队列中的紧急任务，普通任务可能因此延后；已经执行的任务不会被抢占。",
                      "这解释了可能的取舍；是否出现、幅度多大应以以上数据为准。不能据此断言吞吐量提高或满足硬实时。"]
            return "\n".join(lines)
        except (KeyError, TypeError, ValueError):
            return "报告结构无效或不完整，无法得出结论。"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "report", "explain"))
    parser.add_argument("--status", type=Path, default=Path("results/live/status.json"))
    parser.add_argument("--report", type=Path, default=Path("results/comparison/report.json"))
    args = parser.parse_args()
    agent = ReadOnlyAgent(args.status, args.report)
    if args.command == "explain":
        print(agent.explain())
    else:
        print(json.dumps(agent.call("query_status" if args.command == "status" else "read_report"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
