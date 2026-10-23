"""Run paired FIFO/priority experiments against the real C executable."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import time
from .replay import load, replay
from .report import build


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(process, status_path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited early ({process.returncode}); inspect server.log")
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
            if state["running"]:
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(.02)
    raise TimeoutError("server did not become ready")


def wait_complete(process, directory, expected, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("server exited before all tasks were recorded")
        try:
            status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
            if status["completed"] + status["rejected"] == expected and status["queue_depth"] == 0 and not status["active"]:
                return status
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(.05)
    raise TimeoutError(f"did not finish {expected} messages within {timeout}s")


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise TimeoutError("server failed graceful shutdown")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=Path("linux/build/edge_scheduler"))
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/comparison"))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--capacity", type=int, default=64)
    parser.add_argument("--window", type=int, default=8)
    parser.add_argument("--passes", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--cpu", type=int, help="pin this replay process and its child server to one allowed CPU")
    args = parser.parse_args()
    if min(args.repeats, args.capacity, args.window, args.passes, args.timeout) <= 0:
        parser.error("repeats/capacity/window/passes/timeout must be positive")
    if args.output.exists():
        parser.error("output already exists; use a fresh directory to preserve prior evidence")
    if args.cpu is not None:
        if not hasattr(os, "sched_setaffinity"):
            parser.error("--cpu requires Linux affinity support")
        os.sched_setaffinity(0, {args.cpu})
    records = load(args.workload)
    server = str(args.server.resolve(strict=True))
    conditions = {"capacity": args.capacity, "window": args.window, "passes": args.passes,
                  "worker_threads": 1, "platform": platform.platform(),
                  "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
                  "server_sha256": hashlib.sha256(Path(server).read_bytes()).hexdigest()}
    workload_hash = hashlib.sha256(args.workload.read_bytes()).hexdigest()
    args.output.mkdir(parents=True)
    for repeat in range(args.repeats):
        # Reverse order on alternating pairs to reduce simple warm-up/order bias.
        for policy in (("fifo", "priority") if repeat % 2 == 0 else ("priority", "fifo")):
            directory = args.output / f"run-{repeat+1:02d}-{policy}"
            directory.mkdir()
            port = free_port()
            command = [server, "--bind", "127.0.0.1", "--port", str(port), "--policy", policy,
                       "--capacity", str(args.capacity), "--window", str(args.window),
                       "--passes", str(args.passes), "--output", str(directory)]
            with (directory / "server.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                try:
                    wait_ready(process, directory / "status.json")
                    # status may precede listen by a few instructions; make a bounded connection probe.
                    for attempt in range(100):
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=.2):
                                pass
                            break
                        except OSError:
                            if process.poll() is not None or attempt == 99:
                                raise
                            time.sleep(.02)
                    sending = replay(records, "127.0.0.1", port)
                    final = wait_complete(process, directory, len(records), args.timeout)
                finally:
                    stop(process)
                if process.returncode:
                    raise RuntimeError(f"server exited with {process.returncode}")
            meta = {"policy": policy, "repeat": repeat+1, "workload_sha256": workload_hash,
                    "conditions": conditions, "replay": sending, "final_status": final,
                    "command": command, "data_source": "synthetic"}
            (directory / "metadata.json").write_text(json.dumps(meta, indent=2)+"\n", encoding="utf-8")
            print(f"{directory.name}: completed={final['completed']} rejected={final['rejected']}", flush=True)
    build(args.output)
    print(args.output / "report.md")


if __name__ == "__main__":
    main()
