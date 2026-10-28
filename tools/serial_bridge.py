"""Forward bounded, validated USB serial NDJSON to the Linux TCP service.

Install optional hardware dependency with: pip install -r requirements-device.txt
A successful send is NOT an application ACK. Uncertain sends are never replayed.
"""
import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import socket
import time

from .protocol import MAX_FRAME, encode, validate_message


class LineFramer:
    """Keep at most max_frame + 1 bytes (optional CR); discard oversize through LF."""
    def __init__(self, max_frame=8192):
        if not 1 <= max_frame <= MAX_FRAME:
            raise ValueError("max_frame must be between 1 and protocol MAX_FRAME")
        self.max_frame = max_frame
        self.pending = bytearray()
        self.discarding = False
        self.oversized = 0

    def feed(self, chunk):
        for byte in chunk:
            if byte == 10:
                frame = None if self.discarding else bytes(self.pending).removesuffix(b"\r")
                self.pending.clear()
                self.discarding = False
                if frame is not None:
                    yield frame
            elif not self.discarding:
                # One extra trailing CR supports exact-limit CRLF frames.
                if len(self.pending) >= self.max_frame and not (len(self.pending) == self.max_frame and byte == 13):
                    self.oversized += 1
                    self.pending.clear()
                    self.discarding = True
                else:
                    self.pending.append(byte)

    def finish(self):
        """On disconnect/exit, discard a partial line; never join across sessions."""
        incomplete = bool(self.pending)
        self.pending.clear()
        self.discarding = False
        return int(incomplete)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def validated_frame(line):
    """Reject duplicate/unknown fields and non-ASCII before canonical encoding."""
    message = json.loads(line.decode("ascii"), object_pairs_hook=_unique_object)
    validate_message(message)
    return encode(message)


@dataclass
class BridgeConfig:
    port: str
    output: Path
    baud: int = 115200
    host: str = "127.0.0.1"
    tcp_port: int = 9000
    max_frame: int = 8192
    reconnect_delay: float = 1.0
    limit: int | None = None
    duration: float | None = None

    def validate(self):
        if not self.port or not self.host or not 1 <= self.tcp_port <= 65535:
            raise ValueError("port/host and a valid TCP port are required")
        if self.baud <= 0 or not 1 <= self.max_frame <= MAX_FRAME:
            raise ValueError("invalid baud/max-frame")
        if not math.isfinite(self.reconnect_delay) or not 0.05 <= self.reconnect_delay <= 60:
            raise ValueError("reconnect-delay must be within 0.05..60 seconds")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be positive")
        if self.duration is not None and (not math.isfinite(self.duration) or self.duration <= 0):
            raise ValueError("duration must be finite and positive")


def _close(resource):
    if resource is not None:
        try:
            resource.close()
        except OSError:
            pass


def run_bridge(config, serial_factory, socket_factory=socket.create_connection,
               clock=time.monotonic, sleep=time.sleep):
    """Run one serial connection and one TCP connection with no task backlog.

    serial_factory accepts (port, baudrate=..., timeout=...). Inject factories in
    tests; real hardware uses serial.Serial. Output directory must be new.
    """
    config.validate()
    directory = Path(config.output)
    directory.mkdir(parents=True, exist_ok=False)
    framer = LineFramer(config.max_frame)
    stats = {"forwarded": 0, "invalid": 0, "oversized": 0, "incomplete": 0,
             "serial_opens": 0, "serial_reconnects": 0, "serial_errors": 0,
             "tcp_connections": 0, "tcp_errors": 0, "dropped": 0,
             "uncertain": 0, "running": True, "port": config.port,
             "baud": config.baud, "host": config.host, "tcp_port": config.tcp_port,
             "max_frame": config.max_frame,
             "forwarded_semantics": "sendall completed, NOT server accepted/completed",
             "last_error": None}
    started = clock()
    last_saved = started
    serial_port = tcp = None

    def expired():
        return config.duration is not None and clock() - started >= config.duration

    def save():
        nonlocal last_saved
        stats["oversized"] = framer.oversized
        stats["elapsed_seconds"] = round(clock() - started, 3)
        temporary = directory / "stats.json.tmp"
        temporary.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(directory / "stats.json")
        last_saved = clock()

    def pause():
        delay = config.reconnect_delay
        if config.duration is not None:
            delay = min(delay, max(0.0, config.duration - (clock() - started)))
        sleep(delay)

    save()
    try:
        with (directory / "frames.jsonl").open("wb") as capture:
            while not expired() and (config.limit is None or stats["forwarded"] < config.limit):
                if serial_port is None:
                    try:
                        serial_port = serial_factory(config.port, baudrate=config.baud, timeout=0.5)
                        stats["serial_opens"] += 1
                        stats["serial_reconnects"] = stats["serial_opens"] - 1
                    except OSError as error:
                        stats["serial_errors"] += 1
                        stats["last_error"] = "serial open: " + str(error)
                        save()
                        pause()
                        continue
                try:
                    read_size = min(4096, max(1, getattr(serial_port, "in_waiting", 1)))
                    chunk = serial_port.read(read_size)
                except OSError as error:
                    stats["serial_errors"] += 1
                    stats["incomplete"] += framer.finish()
                    stats["last_error"] = "serial read: " + str(error)
                    _close(serial_port)
                    serial_port = None
                    save()
                    pause()
                    continue
                for line in framer.feed(chunk):
                    if expired():
                        break
                    try:
                        frame = validated_frame(line)
                    except (ValueError, UnicodeError, RecursionError, OverflowError):
                        stats["invalid"] += 1
                        continue
                    if tcp is None:
                        try:
                            tcp = socket_factory((config.host, config.tcp_port), timeout=3.0)
                            tcp.settimeout(3.0)
                            stats["tcp_connections"] += 1
                        except OSError as error:
                            _close(tcp)
                            tcp = None
                            stats["tcp_errors"] += 1
                            stats["dropped"] += 1
                            stats["last_error"] = "TCP connect: " + str(error)
                            save()
                            pause()
                            continue
                    try:
                        tcp.sendall(frame)
                    except OSError as error:
                        # Some bytes might have reached the server: do not replay.
                        stats["tcp_errors"] += 1
                        stats["dropped"] += 1
                        stats["uncertain"] += 1
                        stats["last_error"] = "TCP send (not replayed): " + str(error)
                        _close(tcp)
                        tcp = None
                        save()
                        pause()
                        continue
                    capture.write(frame)
                    capture.flush()
                    stats["forwarded"] += 1
                    if config.limit is not None and stats["forwarded"] >= config.limit:
                        break
                if clock() - last_saved >= 1:
                    save()
    except KeyboardInterrupt:
        stats["last_error"] = "stopped by user"
    finally:
        stats["incomplete"] += framer.finish()
        stats["running"] = False
        if tcp is not None:
            try:
                tcp.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        _close(tcp)
        _close(serial_port)
        save()
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="actual CH340 COM port, or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tcp-port", type=int, default=9000)
    parser.add_argument("--max-frame", type=int, default=8192)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--limit", type=int, help="stop after this many successful sends")
    parser.add_argument("--duration", type=float, help="stop after this many seconds, even if disconnected")
    parser.add_argument("--reconnect-delay", type=float, default=1.0)
    args = parser.parse_args()
    config = BridgeConfig(**vars(args))
    try:
        config.validate()
        import serial  # Optional: importing other project tools needs no pyserial.
        result = run_bridge(config, serial.Serial)
    except ImportError:
        parser.exit(1, "Install hardware dependency: python -m pip install -r requirements-device.txt\n")
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if config.limit is not None and result["forwarded"] < config.limit:
        parser.exit(1, "Stopped before requested --limit was reached; inspect stats.json.\n")


if __name__ == "__main__":
    main()
