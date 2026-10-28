"""Optional Matplotlib export of measured results (core tools need only stdlib)."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not report.get("comparable_configuration"):
        parser.error("report configurations are not comparable")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False,
                         "axes.spines.right": False, "svg.fonttype": "none"})
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.5), layout="constrained")
    for index, priority in enumerate(("urgent", "normal")):
        ax = axes[index]
        values = [report["pooled"][policy][priority]["wait_ms"]["p95"] for policy in ("fifo", "priority")]
        all_heights = [v or 0 for v in values]
        for position, (policy, value, color) in enumerate(zip(("FIFO", "Priority"), values, ("#466C95", "#CE7E35"))):
            if value is None:
                ax.text(position, 0, "No completed tasks", rotation=90, va="bottom", ha="center")
            else:
                ax.bar(position, value, color=color, width=.6)
            # Dots show each run's P95, not a confidence interval.
            per_run = [r["metrics"][priority]["wait_ms"]["p95"] for r in report["runs"]
                       if r["policy"] == policy.lower()]
            observed = [v for v in per_run if v is not None]
            all_heights.extend(observed)
            ax.scatter([position-.10]*len(observed), observed, color="#1E2936", s=18, zorder=3)
            if value is not None:
                ax.annotate(f"{value:.2f}", (position, max([value]+observed)), xytext=(0, 7), textcoords="offset points", ha="center")
        ax.set_xticks([0, 1], ["FIFO", "Priority"])
        ax.set_title(f"{priority.title()} tasks")
        ax.set_ylabel("Queue wait P95 (ms)")
        ax.set_ylim(bottom=0, top=max(all_heights+[1])*1.22)
        ax.grid(axis="y", alpha=.15)
        ax.set_axisbelow(True)
    labels, completed, rejected = [], [], []
    for policy in ("fifo", "priority"):
        for priority in ("urgent", "normal"):
            metrics = report["pooled"][policy][priority]
            labels.append(f"{policy}\n{priority}")
            completed.append(metrics["completed"])
            rejected.append(metrics["rejected"])
    axes[2].bar(range(4), completed, label="Completed", color="#437E72")
    axes[2].bar(range(4), rejected, bottom=completed, label="Rejected", color="#B94F57", hatch="//")
    for i, (count, dropped) in enumerate(zip(completed, rejected)):
        axes[2].text(i, count+dropped+.5, f"{count}/{dropped}", ha="center", fontsize=9)
    axes[2].set_xticks(range(4), labels, fontsize=9)
    axes[2].set_ylabel("Task count (all runs)")
    axes[2].set_title("Completion / rejection")
    axes[2].set_ylim(0, max([c+r for c, r in zip(completed, rejected)]+[1])*1.2)
    axes[2].legend(loc="upper right", fontsize=8)
    fig.suptitle("Synthetic workload · Measured Linux results\nBars: pooled P95; dots: individual runs. Rejected tasks excluded from latency.", fontsize=12)
    for suffix in ("png", "svg"):
        path = args.report.with_name(f"comparison.{suffix}")
        fig.savefig(path, dpi=180)
        print(path)
    plt.close(fig)


if __name__ == "__main__":
    main()
