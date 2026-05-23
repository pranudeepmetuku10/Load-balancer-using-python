"""Tiny test backend. Run several on different ports:

    python backend.py 9001
    python backend.py 9002
    python backend.py 9003

Each instance:
  - GET /        -> "hello from :PORT"
  - GET /health  -> 200 if healthy, 503 if toggled off
  - POST /toggle -> flip health state (for testing the LB's reaction)
"""
import sys

from aiohttp import web

healthy = True


async def root(request: web.Request) -> web.Response:
    return web.Response(text=f"hello from :{request.app['port']}  path={request.path}\n")


async def health(request: web.Request) -> web.Response:
    return web.Response(status=200 if healthy else 503, text="ok" if healthy else "down")


async def toggle(request: web.Request) -> web.Response:
    global healthy
    healthy = not healthy
    return web.Response(text=f"healthy={healthy}\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9001
    app = web.Application()
    app["port"] = port
    app.router.add_get("/health", health)
    app.router.add_post("/toggle", toggle)
    app.router.add_route("*", "/{tail:.*}", root)
    web.run_app(app, port=port)
