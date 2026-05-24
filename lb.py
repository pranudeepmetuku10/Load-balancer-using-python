"""HTTP reverse-proxy load balancer.

Strategies:  round-robin | least-connections | weighted (smooth WRR)
Health:      active probing of /health every HEALTH_INTERVAL seconds
Run:         python lb.py --strategy round-robin
             python lb.py --strategy least-connections
             python lb.py --strategy weighted
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

# (url, weight). Weight is used only by the weighted strategy; ignored otherwise.
BACKENDS: list[tuple[str, int]] = [
    ("http://localhost:9001", 1),
    ("http://localhost:9002", 2),
    ("http://localhost:9003", 3),
]

HEALTH_PATH = "/health"
HEALTH_INTERVAL = 5.0
HEALTH_TIMEOUT = 2.0

# Hop-by-hop headers must not be forwarded across a proxy (RFC 7230 §6.1).
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
})

log = logging.getLogger("lb")

# --- Prometheus metrics -----------------------------------------------------
REQUESTS = Counter(
    "lb_requests_total",
    "Requests forwarded by the load balancer.",
    ["backend", "method", "status"],
)
LATENCY = Histogram(
    "lb_request_duration_seconds",
    "End-to-end proxy latency per backend.",
    ["backend"],
)
ACTIVE = Gauge(
    "lb_active_requests",
    "In-flight requests currently being proxied.",
    ["backend"],
)
HEALTHY = Gauge(
    "lb_backend_healthy",
    "1 if the backend is currently healthy, else 0.",
    ["backend"],
)
PICKS = Counter(
    "lb_picks_total",
    "Times the balancer selected a given backend.",
    ["backend", "strategy"],
)
ERRORS = Counter(
    "lb_upstream_errors_total",
    "Upstream errors hitting a backend (network failures, 5xx are NOT counted here).",
    ["backend"],
)


@dataclass
class Backend:
    url: str
    weight: int = 1
    healthy: bool = True
    active: int = 0          # in-flight requests, used by least-connections
    _cw: int = field(default=0, repr=False)  # current weight, used by smooth WRR


class Strategy(Protocol):
    def pick(self) -> Backend | None: ...


class RoundRobin:
    def __init__(self, backends: list[Backend]) -> None:
        self.backends = backends
        self._idx = 0

    def pick(self) -> Backend | None:
        n = len(self.backends)
        for _ in range(n):
            b = self.backends[self._idx % n]
            self._idx += 1
            if b.healthy:
                return b
        return None


class LeastConnections:
    def __init__(self, backends: list[Backend]) -> None:
        self.backends = backends

    def pick(self) -> Backend | None:
        healthy = [b for b in self.backends if b.healthy]
        if not healthy:
            return None
        # Tie-break by url so the choice is deterministic when counts are equal.
        return min(healthy, key=lambda b: (b.active, b.url))


class WeightedRoundRobin:
    """Smooth weighted round robin (the algorithm nginx uses).

    On each pick: every healthy backend's current weight grows by its configured
    weight; the largest current weight wins and is then decreased by the total
    weight of healthy backends. Distribution converges to the weight ratios
    without bursty batching.
    """

    def __init__(self, backends: list[Backend]) -> None:
        self.backends = backends

    def pick(self) -> Backend | None:
        total = 0
        best: Backend | None = None
        for b in self.backends:
            if not b.healthy:
                continue
            b._cw += b.weight
            total += b.weight
            if best is None or b._cw > best._cw:
                best = b
        if best is None:
            return None
        best._cw -= total
        return best


STRATEGIES: dict[str, type[Strategy]] = {
    "round-robin": RoundRobin,
    "least-connections": LeastConnections,
    "weighted": WeightedRoundRobin,
}


async def health_loop(backends: list[Backend], session: ClientSession) -> None:
    timeout = ClientTimeout(total=HEALTH_TIMEOUT)
    while True:
        await asyncio.gather(*(_probe(b, session, timeout) for b in backends))
        await asyncio.sleep(HEALTH_INTERVAL)


async def _probe(backend: Backend, session: ClientSession, timeout: ClientTimeout) -> None:
    was_healthy = backend.healthy
    try:
        async with session.get(backend.url + HEALTH_PATH, timeout=timeout) as resp:
            backend.healthy = resp.status == 200
    except (ClientError, asyncio.TimeoutError):
        backend.healthy = False
    HEALTHY.labels(backend=backend.url).set(1 if backend.healthy else 0)
    if was_healthy != backend.healthy:
        log.info("backend %s -> %s", backend.url, "UP" if backend.healthy else "DOWN")


async def proxy(request: web.Request) -> web.StreamResponse:
    balancer: Strategy = request.app["balancer"]
    session: ClientSession = request.app["session"]
    strategy_name: str = request.app["strategy_name"]

    backend = balancer.pick()
    if backend is None:
        REQUESTS.labels(backend="none", method=request.method, status="503").inc()
        return web.Response(status=503, text="no healthy backends")

    PICKS.labels(backend=backend.url, strategy=strategy_name).inc()

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    target = backend.url + request.rel_url.raw_path_qs
    body = await request.read()

    backend.active += 1
    ACTIVE.labels(backend=backend.url).inc()
    status = 502
    start = asyncio.get_event_loop().time()
    try:
        async with session.request(
            request.method, target, headers=headers, data=body, allow_redirects=False,
        ) as upstream:
            status = upstream.status
            resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
            response = web.StreamResponse(status=upstream.status, headers=resp_headers)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response
    except ClientError as e:
        log.warning("upstream error from %s: %s", backend.url, e)
        backend.healthy = False
        HEALTHY.labels(backend=backend.url).set(0)
        ERRORS.labels(backend=backend.url).inc()
        return web.Response(status=502, text=f"bad gateway: {e}")
    finally:
        backend.active -= 1
        ACTIVE.labels(backend=backend.url).dec()
        LATENCY.labels(backend=backend.url).observe(asyncio.get_event_loop().time() - start)
        REQUESTS.labels(backend=backend.url, method=request.method, status=str(status)).inc()


async def metrics(request: web.Request) -> web.Response:
    return web.Response(body=generate_latest(REGISTRY), content_type=CONTENT_TYPE_LATEST.split(";")[0])


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession()
    app["health_task"] = asyncio.create_task(health_loop(app["backends"], app["session"]))


async def on_cleanup(app: web.Application) -> None:
    app["health_task"].cancel()
    try:
        await app["health_task"]
    except asyncio.CancelledError:
        pass
    await app["session"].close()


def build_app(strategy_name: str = "round-robin") -> web.Application:
    backends = [Backend(url=url, weight=w) for url, w in BACKENDS]
    strategy_cls = STRATEGIES[strategy_name]
    app = web.Application()
    app["backends"] = backends
    app["balancer"] = strategy_cls(backends)
    app["strategy_name"] = strategy_name
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    # /metrics is registered before the catch-all so it isn't proxied.
    app.router.add_get("/metrics", metrics)
    app.router.add_route("*", "/{tail:.*}", proxy)
    # Initialize health gauges so Prometheus has them on first scrape.
    for b in backends:
        HEALTHY.labels(backend=b.url).set(1 if b.healthy else 0)
    return app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simple HTTP load balancer.")
    p.add_argument("--strategy", choices=sorted(STRATEGIES), default="round-robin",
                   help="Load-balancing strategy (default: round-robin).")
    p.add_argument("--host", default=LISTEN_HOST)
    p.add_argument("--port", type=int, default=LISTEN_PORT)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log.info("starting LB on %s:%d  strategy=%s  backends=%s",
             args.host, args.port, args.strategy, [(u, w) for u, w in BACKENDS])
    web.run_app(build_app(args.strategy), host=args.host, port=args.port, print=None)
