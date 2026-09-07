#!/usr/bin/env python3
"""Forward a local TCP port to the host ADB server for Chromedriver.

Appium can connect to a remote ADB server through capabilities, while
Chromedriver still expects ADB on 127.0.0.1:5037. This small full-duplex proxy
keeps both clients pointed at the same host-side ADB server.
"""
from __future__ import annotations

import argparse
import logging
import re
import selectors
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request


logger = logging.getLogger('adb-proxy')
FORWARD_PORT_RESPONSE_RE = re.compile(
    rb'(?:OKAY)+(?:[0-9a-fA-F]{4})([0-9]{1,5})'
)
KILL_FORWARD_REQUEST_RE = re.compile(r':killforward:tcp:(\d+)$')
KILL_FORWARD_REQUEST_BYTES_RE = re.compile(rb':killforward:tcp:(\d+)')
EXPLICIT_FORWARD_REQUEST_BYTES_RE = re.compile(
    rb':forward:(?:norebind:)?tcp:(\d+);'
)


def relay_sockets(left: socket.socket, right: socket.socket, observer=None) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            for key, _ in selector.select():
                data = key.fileobj.recv(65536)
                if not data:
                    return
                if observer is not None:
                    observer(key.fileobj, data)
                key.data.sendall(data)
    finally:
        selector.close()


def extract_adb_service(buffer: bytes) -> str:
    if len(buffer) < 4:
        return ''
    try:
        service_length = int(buffer[:4], 16)
    except ValueError:
        return ''
    if service_length <= 0 or service_length > 65536:
        return ''
    if len(buffer) < 4 + service_length:
        return ''
    return buffer[4:4 + service_length].decode('utf-8', errors='replace')


class TcpForwardHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.create_connection(
            (self.server.upstream_host, self.server.upstream_port),
            timeout=10,
        )
        try:
            relay_sockets(self.request, upstream)
        finally:
            upstream.close()


class TcpForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, upstream_host: str, upstream_port: int):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        super().__init__(server_address, TcpForwardHandler)


class AdbProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = self.server.open_upstream()
        try:
            request_buffer = bytearray()
            response_buffer = bytearray()
            state = {
                'service': '',
                'is_dynamic_forward': False,
                'dynamic_forward_created': False,
                'kill_forward_handled': False,
            }

            def observe(source: socket.socket, data: bytes) -> None:
                if source is self.request:
                    request_buffer.extend(data)
                    if not state['service']:
                        state['service'] = extract_adb_service(request_buffer)
                    if b':forward:tcp:0;' in request_buffer:
                        state['is_dynamic_forward'] = True
                    explicit_forward_match = EXPLICIT_FORWARD_REQUEST_BYTES_RE.search(
                        request_buffer
                    )
                    if (
                        explicit_forward_match
                        and not state['dynamic_forward_created']
                    ):
                        explicit_port = int(explicit_forward_match.group(1))
                        if explicit_port > 0:
                            self.server.add_dynamic_forward(explicit_port)
                            state['dynamic_forward_created'] = True
                    if not state['kill_forward_handled']:
                        kill_match = KILL_FORWARD_REQUEST_RE.search(state['service'])
                        if kill_match is None:
                            raw_kill_match = KILL_FORWARD_REQUEST_BYTES_RE.search(request_buffer)
                            kill_match = raw_kill_match
                        if kill_match:
                            self.server.remove_dynamic_forward(int(kill_match.group(1)))
                            state['kill_forward_handled'] = True
                    return

                if (
                    source is upstream
                    and state['is_dynamic_forward']
                    and not state['dynamic_forward_created']
                ):
                    response_buffer.extend(data)
                    match = FORWARD_PORT_RESPONSE_RE.search(response_buffer)
                    if match:
                        self.server.add_dynamic_forward(int(match.group(1)))
                        state['dynamic_forward_created'] = True

            relay_sockets(self.request, upstream, observe)
        finally:
            upstream.close()


class AdbProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address,
        handler,
        upstream_host: str,
        upstream_port: int,
        recovery_url: str = '',
    ):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        self.recovery_url = recovery_url.strip()
        self.listen_host = server_address[0]
        self._dynamic_forwards = {}
        self._dynamic_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._last_recovery_at = 0.0
        super().__init__(server_address, handler)

    def open_upstream(self) -> socket.socket:
        try:
            return socket.create_connection(
                (self.upstream_host, self.upstream_port),
                timeout=10,
            )
        except OSError as exc:
            last_error = exc
            if not self.recover_upstream(exc):
                raise

        for _ in range(10):
            time.sleep(0.2)
            try:
                return socket.create_connection(
                    (self.upstream_host, self.upstream_port),
                    timeout=10,
                )
            except OSError as exc:
                last_error = exc
        raise last_error

    def recover_upstream(self, cause: OSError) -> bool:
        if not self.recovery_url:
            return False

        with self._recovery_lock:
            now = time.monotonic()
            if now - self._last_recovery_at < 1.0:
                return True

            logger.warning(
                'ADB upstream %s:%s unavailable (%s); requesting recovery via %s',
                self.upstream_host,
                self.upstream_port,
                cause,
                self.recovery_url,
            )
            request = urllib.request.Request(
                self.recovery_url,
                data=b'{}',
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    if not 200 <= response.status < 300:
                        logger.error('ADB recovery returned HTTP %s', response.status)
                        return False
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.error('ADB recovery request failed: %s', exc)
                return False

            self._last_recovery_at = time.monotonic()
            logger.info('ADB upstream recovery requested successfully')
            return True

    def add_dynamic_forward(self, port: int) -> None:
        with self._dynamic_lock:
            if port in self._dynamic_forwards:
                return
            try:
                proxy = TcpForwardServer(
                    (self.listen_host, port),
                    self.upstream_host,
                    port,
                )
            except OSError as exc:
                logger.warning('Unable to proxy ADB forward port %s: %s', port, exc)
                return
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            self._dynamic_forwards[port] = proxy
            logger.info(
                'ADB dynamic forward listening on %s:%s -> %s:%s',
                self.listen_host,
                port,
                self.upstream_host,
                port,
            )

    def remove_dynamic_forward(self, port: int) -> None:
        with self._dynamic_lock:
            proxy = self._dynamic_forwards.pop(port, None)
        if proxy is None:
            return
        threading.Thread(
            target=self._close_dynamic_forward,
            args=(port, proxy),
            daemon=True,
        ).start()

    @staticmethod
    def _close_dynamic_forward(port: int, proxy: TcpForwardServer) -> None:
        proxy.shutdown()
        proxy.server_close()
        logger.info('ADB dynamic forward stopped on 127.0.0.1:%s', port)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--listen-host', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, default=5037)
    parser.add_argument('--upstream-host', required=True)
    parser.add_argument('--upstream-port', type=int, default=5037)
    parser.add_argument('--recovery-url', default='')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    server = AdbProxyServer(
        (args.listen_host, args.listen_port),
        AdbProxyHandler,
        args.upstream_host,
        args.upstream_port,
        recovery_url=args.recovery_url,
    )
    logger.info(
        'ADB proxy listening on %s:%s -> %s:%s',
        args.listen_host,
        args.listen_port,
        args.upstream_host,
        args.upstream_port,
    )
    server.serve_forever()


if __name__ == '__main__':
    main()
