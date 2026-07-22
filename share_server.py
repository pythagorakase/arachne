#!/usr/bin/env python3
"""Narrow public origin for inert Arachne decision snapshots.

This process deliberately knows nothing about pages, sessions, rulings, or the
owner token. A public reverse proxy may expose it because its route set is only
health plus opaque capability snapshots from the dedicated share store.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from share_store import SHARE_ID, ShareStore


LOGGER = logging.getLogger("arachne.share")
LOOPBACK_HOST = "127.0.0.1"
MAX_CONNECTION_WORKERS = 64
SHARE_PATH = re.compile(r"/s/([A-Za-z0-9_-]{32})(\.md)?\Z")
PUBLIC_SHARE_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "sandbox"
)
SHARE_LOG_ID = re.compile(r"(/s/)[A-Za-z0-9_-]{32}")


@dataclass(frozen=True)
class Config:
    share_dir: Path
    port: int

    @classmethod
    def from_environment(cls) -> "Config":
        default_state = Path.home() / ".local" / "state" / "arachne"
        data_dir = Path(os.environ.get("ARACHNE_DATA_DIR", default_state)).expanduser()
        share_dir = Path(
            os.environ.get("ARACHNE_SHARE_DIR", data_dir / "shares")
        ).expanduser()
        port_text = os.environ.get("ARACHNE_SHARE_PORT", "8791")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(
                f"ARACHNE_SHARE_PORT must be an integer, got {port_text!r}"
            ) from exc
        if not 0 <= port <= 65535:
            raise ValueError("ARACHNE_SHARE_PORT must be between 0 and 65535")
        return cls(share_dir=share_dir.absolute(), port=port)


class PublicShareServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, store: ShareStore) -> None:
        self.config = config
        self.store = store
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTION_WORKERS)
        super().__init__((LOOPBACK_HOST, config.port), PublicShareHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class PublicShareHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ArachneShare/1"
    timeout = 20

    def version_string(self) -> str:
        return self.server_version

    @property
    def shares(self) -> PublicShareServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        message = SHARE_LOG_ID.sub(r"\1<redacted>", fmt % args)
        LOGGER.info("%s - %s", self.client_address[0], message)

    def _write(
        self,
        status: HTTPStatus,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        *,
        head_only: bool = False,
        filename: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", PUBLIC_SHARE_CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive, nosnippet")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _not_found(self, *, head_only: bool = False) -> None:
        body = b'{"error":"not_found"}\n'
        self._write(HTTPStatus.NOT_FOUND, body, head_only=head_only)

    def _dispatch(self, *, head_only: bool) -> None:
        transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
        lengths = self.headers.get_all("Content-Length", [])
        if transfer_encoding or len(lengths) > 1 or (lengths and lengths[0] != "0"):
            self.close_connection = True
            body = b'{"error":"unexpected_request_body"}\n'
            self._write(HTTPStatus.BAD_REQUEST, body, head_only=head_only)
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if parsed.query or parsed.fragment:
            self._not_found(head_only=head_only)
            return
        if path == "/health":
            body = b'{"ok":true}\n'
            self._write(HTTPStatus.OK, body, head_only=head_only)
            return
        match = SHARE_PATH.fullmatch(path)
        if match is None:
            self._not_found(head_only=head_only)
            return
        share_id, markdown_suffix = match.groups()
        if SHARE_ID.fullmatch(share_id) is None:
            self._not_found(head_only=head_only)
            return
        format_name = "markdown" if markdown_suffix else "html"
        record = self.shares.store.read(share_id, format_name)
        if record is None:
            self._not_found(head_only=head_only)
            return
        share, body = record
        extension = "md" if format_name == "markdown" else "html"
        content_type = (
            "text/markdown; charset=utf-8"
            if format_name == "markdown"
            else "text/html; charset=utf-8"
        )
        safe_issue = re.sub(r"[^A-Za-z0-9._-]+", "-", share.issue).strip("-._")
        safe_issue = safe_issue[:80]
        filename = f"arachne-{safe_issue or 'decision'}.{extension}"
        self._write(
            HTTPStatus.OK,
            body,
            content_type,
            head_only=head_only,
            filename=filename,
        )

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(head_only=True)

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        body = b'{"error":"method_not_allowed"}\n'
        self._write(
            HTTPStatus.METHOD_NOT_ALLOWED,
            body,
            extra_headers={"Allow": "GET, HEAD"},
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()

    def do_TRACE(self) -> None:  # noqa: N802 - stdlib handler API
        self._method_not_allowed()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ARACHNE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_environment()
    store = ShareStore(config.share_dir)
    server = PublicShareServer(config, store)
    stop_requested = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s; stopping", signum)
        stop_requested.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print(
        json.dumps(
            {
                "event": "started",
                "host": LOOPBACK_HOST,
                "port": server.server_port,
                "share_dir": str(config.share_dir),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0 if stop_requested.is_set() else 1


if __name__ == "__main__":
    raise SystemExit(main())
