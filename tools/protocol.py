"""Shared wire contract and independent numerical reference."""
import json
import math
import re

MAX_SAMPLES = 8192
MAX_FRAME = 262144


def validate_message(message):
    required = {"device_id", "seq", "priority", "source", "samples"}
    if not isinstance(message, dict) or set(message) != required:
        raise ValueError("message must contain exactly the five protocol fields")
    if not isinstance(message["device_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", message["device_id"]):
        raise ValueError("invalid device_id")
    if type(message["seq"]) is not int or not 0 <= message["seq"] <= 2**53 - 1:
        raise ValueError("invalid seq")
    if message["priority"] not in ("normal", "urgent") or message["source"] != "synthetic":
        raise ValueError("unsupported priority/source")
    samples = message["samples"]
    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_SAMPLES:
        raise ValueError("invalid sample count")
    if any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 1e6 for x in samples):
        raise ValueError("samples must be finite numbers with absolute value <= 1e6")
    return message


def encode(message):
    data = json.dumps(validate_message(message), separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
    if len(data) - 1 > MAX_FRAME:
        raise ValueError("frame too large")
    return data


def reference(samples, window=8):
    """Causal moving average, shortened prefix window; RMS/mean of filtered data."""
    if window < 1 or not samples:
        raise ValueError("positive window and nonempty samples required")
    filtered = [math.fsum(samples[max(0, i-window+1):i+1]) / min(i+1, window)
                for i in range(len(samples))]
    return {"rms": math.sqrt(math.fsum(x*x for x in filtered) / len(filtered)),
            "mean": math.fsum(filtered) / len(filtered)}


def synthetic(seq, count, priority="normal", device_id="replay-01"):
    return {"device_id": device_id, "seq": seq, "priority": priority,
            "source": "synthetic",
            "samples": [round(math.sin((i + seq) * 0.13) * 10 + (i % 7 - 3) * 0.1, 6)
                        for i in range(count)]}
