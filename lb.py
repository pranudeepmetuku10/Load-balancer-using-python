"""Simple HTTP reverse-proxy load balancer.

Strategy:    round robin over healthy backends
Health:      active probing of /health every HEALTH_INTERVAL seconds
Run:         python lb.py
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout, web

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080
BACKENDS = [
    "http://localhost:9001",
    "http://localhost:9002",
    "http://localhost:9003",
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


@dataclass
class Backend:
    url: str
    healthy: bool = True


class RoundRobin:
    def __init__(self, backends: list[Backend]) -> None:
        self.backends = backends
        self._cycle = itertools.cycle(backends)

    def pick(self) -> Backend | None:
        for _ in range(len(self.backends)):
            b = next(self._cycle)
            if b.healthy:
                return b
        return None


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
    if was_healthy != backend.healthy:
        log.info("backend %s -> %s", backend.url, "UP" if backend.healthy else "DOWN")


async def proxy(request: web.Request) -> web.StreamResponse:
    balancer: RoundRobin = request.app["balancer"]
    session: ClientSession = request.app["session"]

    backend = balancer.pick()
    if backend is None:
        return web.Response(status=503, text="no healthy backends")

    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
    target = backend.url + request.rel_url.raw_path_qs
    body = await request.read()

    try:
        async with session.request(
            request.method, target, headers=headers, data=body, allow_redirects=False,
        ) as upstream:
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
        return web.Response(status=502, text=f"bad gateway: {e}")


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


def build_app() -> web.Application:
    backends = [Backend(url) for url in BACKENDS]
    app = web.Application()
    app["backends"] = backends
    app["balancer"] = RoundRobin(backends)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    web.run_app(build_app(), host=LISTEN_HOST, port=LISTEN_PORT)
