"""Compute measured per-run/per-class metrics; never infer missing observations."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics


def distribution(values):
    if not values:
        return {"mean": None, "p95": None, "max": None}
    values = sorted(values)
    return {"mean": statistics.fmean(values), "p95": values[math.ceil(.95*len(values))-1], "max": values[-1]}


def summarize(rows):
    result = {}
    for priority in ("normal", "urgent"):
        selected = [r for r in rows if r["priority"] == priority]
        completed = [r for r in selected if r["status"] == "completed"]
        result[priority] = {"completed": len(completed),
                            "rejected": sum(r["status"] == "rejected" for r in selected)}
        for metric in ("wait_ms", "compute_ms", "total_ms"):
            values = [float(r[metric]) for r in completed]
            if any(not math.isfinite(v) or v < 0 for v in values):
                raise ValueError(f"invalid recorded metric: {metric}")
            result[priority][metric] = distribution(values)
    return result


def build(root):
    root = Path(root)
    runs, pooled = [], {"fifo": [], "priority": []}
    for metadata in sorted(root.glob("run-*/metadata.json")):
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        with (metadata.parent / "tasks.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        pooled[meta["policy"]].extend(rows)
        runs.append({"run": metadata.parent.name, "policy": meta["policy"],
                     "metrics": summarize(rows), "metadata": meta,
                     "unclassified_rejections": sum(r["status"] == "rejected" and r["priority"] not in ("normal", "urgent") for r in rows)})
    if not runs:
        raise ValueError("no completed experiment runs found")
    hashes = {r["metadata"]["workload_sha256"] for r in runs}
    conditions = {json.dumps(r["metadata"]["conditions"], sort_keys=True) for r in runs}
    policies = {r["policy"] for r in runs}
    comparable = len(hashes) == 1 and len(conditions) == 1 and policies == {"fifo", "priority"}
    report = {"schema_version": 1, "data_source": "synthetic", "p95_method": "nearest_rank",
              "comparable_configuration": comparable, "runs": runs,
              "pooled": {policy: summarize(rows) for policy, rows in pooled.items()},
              "limitations": ["P95 is pooled over completed tasks; rejected tasks have no completion latency.",
                              "Identical schedules do not guarantee identical actual arrivals; inspect send lateness and received_ns.",
                              "VM and host scheduling introduce noise; these are local measurements, not hardware or real-time guarantees."]}
    root.joinpath("report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    root.joinpath("report.md").write_text(markdown(report), encoding="utf-8")
    return report


def number(value):
    return "N/A" if value is None else f"{value:.3f}"


def markdown(report):
    lines = ["# FIFO / priority 实测对照报告", "", "数据来源：合成测试数据。P95 使用 nearest-rank；不含拒绝任务。", "",
             f"配置一致性检查：{'通过' if report['comparable_configuration'] else '未通过，不能作公平比较'}。", "",
             "| 策略 | 类型 | 完成 | 拒绝 | 等待均值 ms | 等待 P95 ms | 完成 P95 ms |",
             "|---|---|---:|---:|---:|---:|---:|"]
    for policy, metrics in report["pooled"].items():
        for priority, m in metrics.items():
            lines.append(f"| {policy} | {priority} | {m['completed']} | {m['rejected']} | {number(m['wait_ms']['mean'])} | {number(m['wait_ms']['p95'])} | {number(m['total_ms']['p95'])} |")
    lines += ["", "## 各次运行", "", "| 运行 | 类型 | 等待 P95 ms | 完成 P95 ms | 完成 | 拒绝 |", "|---|---|---:|---:|---:|---:|"]
    for run in report["runs"]:
        for priority, m in run["metrics"].items():
            lines.append(f"| {run['run']} | {priority} | {number(m['wait_ms']['p95'])} | {number(m['total_ms']['p95'])} | {m['completed']} | {m['rejected']} |")
    lines += ["", "优先级只影响下一个任务，不能抢占正在执行的普通任务。普通任务可能等待更久，持续紧急流可能导致饥饿。",
              "相同输入和参数不等于实际到达时刻完全相同；metadata.json 记录发送迟到，CSV 记录 Linux 实际接收时间。",
              "请同时检查每次结果、拒绝量、系统负载，不将虚拟机测量推广为开发板性能。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    build(args.directory)
    print(args.directory / "report.md")


if __name__ == "__main__":
    main()
