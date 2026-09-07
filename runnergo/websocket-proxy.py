#!/usr/bin/env python3
"""
WebSocket proxy: sits between nginx and manage-ws.
Intercepts non-JSON messages (like "Pong-") and wraps them as JSON
so the SPA's JSON.parse() never throws.
"""
import asyncio
import json
import logging
import os
import sys

import websockets
from websockets.server import serve

UPSTREAM = os.environ.get("UPSTREAM", "ws://manage-ws:30000")
LISTEN_PORT = int(os.environ.get("PORT", "30001"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ws-proxy")


def safe_wrap(raw: str) -> str:
    """If raw is not valid JSON, wrap it so JSON.parse won't throw."""
    try:
        json.loads(raw)
        return raw  # already valid JSON
    except (json.JSONDecodeError, ValueError):
        wrapped = json.dumps({"type": "raw", "data": raw})
        log.info("Wrapped non-JSON message: %r -> %s", raw[:50], wrapped[:80])
        return wrapped


async def pipe(src, dst, direction: str, wrap: bool = False):
    """Forward messages from src to dst."""
    try:
        async for msg in src:
            if isinstance(msg, bytes):
                # Binary frame — forward as-is
                await dst.send(msg)
            elif wrap:
                # Text frame from upstream — wrap if not JSON
                await dst.send(safe_wrap(msg))
            else:
                # Text frame from client — forward as-is
                await dst.send(msg)
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log.error("Error in %s pipe: %s", direction, e)
    finally:
        try:
            await dst.close()
        except Exception:
            pass


async def handle_client(client_ws):
    """Handle a single client WebSocket connection."""
    client_addr = client_ws.remote_address if hasattr(client_ws, "remote_address") else "?"
    # Forward the request path to upstream
    path = client_ws.path if hasattr(client_ws, "path") else "/"
    upstream_url = UPSTREAM.rstrip("/") + path
    log.info("Client connected from %s, connecting to %s", client_addr, upstream_url)

    try:
        async with websockets.connect(upstream_url, ping_interval=None, ping_timeout=None) as upstream_ws:
            log.info("Connected to upstream %s", upstream_url)
            # client -> upstream (no wrapping)
            # upstream -> client (wrap non-JSON)
            await asyncio.gather(
                pipe(client_ws, upstream_ws, "client->upstream", wrap=False),
                pipe(upstream_ws, client_ws, "upstream->client", wrap=True),
            )
    except Exception as e:
        log.error("Upstream connection error: %s", e)
    finally:
        log.info("Client %s disconnected", client_addr)


async def main():
    log.info("Starting WebSocket proxy on 0.0.0.0:%d -> %s", LISTEN_PORT, UPSTREAM)
    async with serve(handle_client, "0.0.0.0", LISTEN_PORT, ping_interval=None, ping_timeout=None):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
        sys.exit(0)
