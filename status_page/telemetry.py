"""Simple in-process telemetry counters and timings."""

import threading
import time

_lock = threading.Lock()
_counters = {}
_timings = {}


def incr(name, amount=1):
    with _lock:
        _counters[name] = _counters.get(name, 0) + amount


def observe(name, value):
    with _lock:
        t = _timings.setdefault(
            name, {"count": 0, "sum": 0.0, "min": None, "max": None}
        )
        t["count"] += 1
        t["sum"] += float(value)
        t["min"] = value if t["min"] is None else min(t["min"], value)
        t["max"] = value if t["max"] is None else max(t["max"], value)


def timed_call(name, fn, *args, **kwargs):
    start = time.monotonic()
    try:
        return fn(*args, **kwargs)
    finally:
        observe(name, (time.monotonic() - start) * 1000.0)


def snapshot():
    with _lock:
        counters = dict(_counters)
        timings = {}
        for key, val in _timings.items():
            avg = (val["sum"] / val["count"]) if val["count"] else 0.0
            timings[key] = {
                "count": val["count"],
                "avg_ms": round(avg, 3),
                "min_ms": round(val["min"], 3) if val["min"] is not None else None,
                "max_ms": round(val["max"], 3) if val["max"] is not None else None,
            }
    return {"counters": counters, "timings_ms": timings}
