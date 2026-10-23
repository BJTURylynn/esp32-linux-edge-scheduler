"""Generate deterministic synthetic NDJSON schedules and replay over TCP."""
import argparse
import json
from pathlib import Path
import socket
import time
from .protocol import encode, synthetic


def generate():
    records = [{"at_ms": float(i), "message": synthetic(i, 8192)} for i in range(24)]
    records += [{"at_ms": float(30+i), "message": synthetic(24+i, 64, "urgent")} for i in range(8)]
    return records


def load(path):
    records = []
    previous = -1.0
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            at = record["at_ms"]
            if type(at) not in (int, float) or not 0 <= at <= 3600000 or at < previous:
                raise ValueError("arrival times must be finite, sorted, and within one hour")
            previous = at
            records.append((at / 1000, encode(record["message"])))
    if not records:
        raise ValueError("empty workload")
    return records


def replay(records, host, port, chunk_size=0):
    # Serialization occurs before t0 so it does not distort scheduled arrivals.
    lateness = []
    with socket.create_connection((host, port), timeout=10) as connection:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        start = time.monotonic()
        for at, data in records:
            delay = start + at - time.monotonic()
            if delay > 0:
                time.sleep(delay)  # Arrival timing only; never simulates compute work.
            lateness.append(max(0, time.monotonic() - start - at) * 1000)
            if chunk_size:
                for offset in range(0, len(data), chunk_size):
                    connection.sendall(data[offset:offset+chunk_size])
            else:
                connection.sendall(data)
        connection.shutdown(socket.SHUT_WR)
    return {"sent": len(records), "max_send_lateness_ms": max(lateness),
            "mean_send_lateness_ms": sum(lateness)/len(lateness)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("path", type=Path)
    send = sub.add_parser("send")
    send.add_argument("path", type=Path)
    send.add_argument("--host", default="127.0.0.1")
    send.add_argument("--port", type=int, default=9000)
    send.add_argument("--chunk-size", type=int, default=0)
    args = parser.parse_args()
    if args.command == "generate":
        args.path.parent.mkdir(parents=True, exist_ok=True)
        args.path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in generate()), encoding="utf-8")
        print(f"Wrote 32 synthetic tasks to {args.path}")
    else:
        if args.chunk_size < 0:
            parser.error("chunk-size must be nonnegative")
        print(json.dumps(replay(load(args.path), args.host, args.port, args.chunk_size), indent=2))


if __name__ == "__main__":
    main()
