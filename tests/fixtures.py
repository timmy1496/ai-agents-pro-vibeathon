"""Записані відповіді Prometheus/Loki — щоб RCA-тули і евали бігали без стенду."""
from __future__ import annotations

import time


def prom_range(values: list[float], **labels) -> dict:
    now = int(time.time())
    return {"status": "success", "data": {"resultType": "matrix", "result": [{
        "metric": {"__name__": "x", **labels},
        "values": [[now - 30 * (len(values) - i), str(v)] for i, v in enumerate(values)],
    }]}}


def prom_empty() -> dict:
    return {"status": "success", "data": {"resultType": "matrix", "result": []}}


def loki_lines(lines: list[str], **stream) -> dict:
    now = int(time.time()) * 10**9
    return {"status": "success", "data": {"resultType": "streams", "result": [{
        "stream": {"service": "demo-chaos-svc", **stream},
        "values": [[str(now - i), line] for i, line in enumerate(lines)],
    }]}}


ERROR_LOG_LINES = [
    '{"ts":"2026-09-02T10:00:01Z","level":"error","service":"demo-chaos-svc",'
    '"msg":"unhandled exception in order handler: NullPointer on payment_ref","component":"orders"}',
    '{"ts":"2026-09-02T10:00:02Z","level":"error","service":"demo-chaos-svc",'
    '"msg":"unhandled exception in order handler: NullPointer on payment_ref","component":"orders"}',
    '{"ts":"2026-09-02T10:00:03Z","level":"error","service":"demo-chaos-svc",'
    '"msg":"request failed","path":"/","status":500,"duration_ms":0.4}',
    '{"ts":"2026-09-02T10:00:04Z","level":"error","service":"demo-chaos-svc",'
    '"msg":"request failed","path":"/","status":500,"duration_ms":1.9}',
    '{"ts":"2026-09-02T10:00:05Z","level":"error","service":"demo-chaos-svc",'
    '"msg":"database connection timeout after 2000ms: dial tcp 10.4.2.11:5432: i/o timeout"}',
]
