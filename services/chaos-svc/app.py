"""Демо-сервіс стенду: віддає golden signals і вміє ламатись на вимогу.

Керується через POST /chaos/<mode>. Стан у пам'яті — рестарт контейнера скидає його,
що й потрібно для сценарію OOM.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import random
import sys
import time
import uuid

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE = os.getenv("SERVICE_NAME", "demo-chaos-svc")
VERSION = os.getenv("APP_VERSION", "0.0.0")

# service-мітку навішує Prometheus з scrape target — тут її не дублюємо (інакше exported_service)
REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP latency", ["path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
BUILD = Gauge("app_build_info", "Build info", ["version"])
BUILD.labels(version=VERSION).set(1)

chaos = {"latency_ms": 0, "error_rate": 0.0, "db_down": False}
_ballast: list[bytes] = []  # тримає пам'ять для сценарію OOM

# Витік має пережити рестарт: справжній leak живе в коді, і після OOMKilled
# контейнер піднімається й тече знову. Без цього маркера сценарій дає рівно один
# рестарт, RSS не встигає зіскрапитись, і жоден з алертів на ресурси не спрацьовує.
LEAK_MARKER = pathlib.Path("/tmp/chaos-leak")

app = FastAPI()


def log(level: str, msg: str, **kw) -> None:
    """Структурований JSON-лог у stdout — його забирає promtail → Loki."""
    print(json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level, "service": SERVICE, "version": VERSION, "msg": msg, **kw,
    }), flush=True)


# /metrics і /healthz б'ють раз на 5 секунд кожен і не несуть жодної інформації про
# інцидент — логувати їх означає втопити корисні рядки у власному шумі.
QUIET_PATHS = ("/metrics", "/healthz")


@app.middleware("http")
async def instrument(request: Request, call_next):
    started = time.perf_counter()
    trace_id = request.headers.get("x-trace-id") or uuid.uuid4().hex[:16]
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    path = request.url.path if request.url.path in ("/", "/healthz", "/metrics") else "/other"
    REQUESTS.labels(request.method, path, str(response.status_code)).inc()
    LATENCY.labels(path).observe(elapsed)

    if response.status_code >= 500:
        log("error", "request failed", path=path, status=response.status_code,
            duration_ms=round(elapsed * 1000, 1), trace_id=trace_id)
    elif path not in QUIET_PATHS:
        # Успішні запити теж логуються, і це не косметика. Раніше здоровий сервіс не
        # писав у stdout НІЧОГО: у Loki не існувало навіть стріму demo-chaos-svc, тому
        # ревізія A3 ставила здоровому сервісу F («логів за вікно немає»), а панель логів
        # на дашборді була порожня до першого інциденту. Це той самий клас розбіжності
        # «світ тестів vs світ стенду», що й із сервіс-агностичними правилами алертів.
        log("info", "request", path=path, status=response.status_code,
            duration_ms=round(elapsed * 1000, 1), trace_id=trace_id)
    return response


@app.get("/")
async def root():
    if chaos["db_down"]:
        await asyncio.sleep(2.0)
        log("error", "database connection timeout after 2000ms: dial tcp orders-db:5432: i/o timeout",
            component="db", upstream="orders-db")
        return Response('{"error":"db unavailable"}', status_code=503, media_type="application/json")

    if chaos["latency_ms"]:
        await asyncio.sleep(chaos["latency_ms"] / 1000)

    if random.random() < chaos["error_rate"]:
        log("error", "unhandled exception in order handler: NullPointer on payment_ref",
            component="orders")
        return Response('{"error":"internal"}', status_code=500, media_type="application/json")

    return {"service": SERVICE, "version": VERSION, "ok": True}


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": VERSION}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/chaos/latency")
async def chaos_latency(ms: int = 1500):
    chaos["latency_ms"] = ms
    log("warn", "chaos: latency injection", latency_ms=ms)
    return chaos


@app.post("/chaos/errors")
async def chaos_errors(rate: float = 0.35):
    chaos["error_rate"] = rate
    log("warn", "chaos: error injection", error_rate=rate)
    return chaos


@app.post("/chaos/db-down")
async def chaos_db_down(enabled: bool = True):
    chaos["db_down"] = enabled
    log("warn", "chaos: dependency orders-db down", enabled=enabled)
    return chaos


async def _leak(mb_per_sec: int) -> None:
    while True:
        _ballast.append(b"x" * (mb_per_sec * 1024 * 1024))
        log("warn", "memory ballast grew", mb=len(_ballast) * mb_per_sec)
        await asyncio.sleep(1)


@app.post("/chaos/oom")
async def chaos_oom(mb_per_sec: int = 16):
    """Тече пам'яттю, поки docker не вб'є контейнер по mem_limit → рестарти.

    16 МБ/с, а не 32: при ліміті 256 МБ це ~16 секунд до OOM, тобто три-чотири
    скрейпи Prometheus встигають побачити зростання RSS і підняти HighMemoryUsage
    ще до падіння. На 32 МБ/с сервіс помирав між скрейпами і на графіку не лишалось нічого.
    """
    LEAK_MARKER.write_text(str(mb_per_sec))
    asyncio.create_task(_leak(mb_per_sec))
    log("warn", "chaos: memory leak started", mb_per_sec=mb_per_sec)
    return {"leaking": True, "mb_per_sec": mb_per_sec}


@app.on_event("startup")
async def resume_leak() -> None:
    """Після OOMKilled контейнер стартує знову — і витік має продовжитись."""
    if LEAK_MARKER.exists():
        mb_per_sec = int(LEAK_MARKER.read_text() or 16)
        asyncio.create_task(_leak(mb_per_sec))
        log("warn", "chaos: memory leak resumed after restart", mb_per_sec=mb_per_sec)


@app.post("/chaos/reset")
async def chaos_reset():
    chaos.update(latency_ms=0, error_rate=0.0, db_down=False)
    _ballast.clear()
    with contextlib.suppress(FileNotFoundError):
        LEAK_MARKER.unlink()
    log("info", "chaos: reset")
    return chaos


if __name__ == "__main__":  # мінімальний self-check без стенду
    from fastapi.testclient import TestClient

    c = TestClient(app)
    assert c.get("/").status_code == 200
    c.post("/chaos/errors?rate=1.0")
    assert c.get("/").status_code == 500, "error injection не спрацювала"
    c.post("/chaos/db-down?enabled=true")
    assert c.get("/").status_code == 503, "db-down має віддавати 503"
    c.post("/chaos/reset")
    assert c.get("/").status_code == 200, "reset не зняв chaos"
    body = c.get("/metrics").text
    assert "http_requests_total" in body and "http_request_duration_seconds_bucket" in body
    print("self-check ok", file=sys.stderr)
