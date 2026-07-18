#!/usr/bin/env python3
"""Arachne's loopback-only decision server.

The service deliberately uses only the Python standard library.  Tailscale
Serve owns reachability and authentication; this process never listens on a
non-loopback interface.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


LOGGER = logging.getLogger("arachne")
LOOPBACK_HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 1_048_576
PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html\Z")
ISSUE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class ClientProblem(ValueError):
    """A request error that should be returned to the caller."""

    def __init__(self, status: HTTPStatus, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """Write and fsync a file, then atomically install it at *path*."""

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_issue_slug(issue: str) -> str:
    slug = ISSUE_SLUG.sub("-", issue).strip("-._")
    return (slug or "ruling")[:80]


@dataclass(frozen=True)
class Config:
    pages_dir: Path
    data_dir: Path
    port: int
    wait_seconds: float

    @classmethod
    def from_environment(cls) -> "Config":
        source_dir = Path(__file__).resolve().parent
        pages_dir = Path(
            os.environ.get("ARACHNE_PAGES_DIR", source_dir / "pages")
        ).expanduser()
        default_state = Path.home() / ".local" / "state" / "arachne"
        data_dir = Path(
            os.environ.get(
                "ARACHNE_DATA_DIR",
                os.environ.get("BEAN_DIR", default_state),
            )
        ).expanduser()
        port_text = os.environ.get("ARACHNE_PORT", os.environ.get("BEAN_PORT", "8788"))
        wait_text = os.environ.get("ARACHNE_WAIT_SECONDS", "540")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"ARACHNE_PORT must be an integer, got {port_text!r}") from exc
        if not 0 <= port <= 65535:
            raise ValueError("ARACHNE_PORT must be between 0 and 65535")
        try:
            wait_seconds = float(wait_text)
        except ValueError as exc:
            raise ValueError(
                f"ARACHNE_WAIT_SECONDS must be numeric, got {wait_text!r}"
            ) from exc
        if not 0.05 <= wait_seconds <= 600:
            raise ValueError("ARACHNE_WAIT_SECONDS must be between 0.05 and 600")
        return cls(
            pages_dir=pages_dir.resolve(),
            data_dir=data_dir.resolve(),
            port=port,
            wait_seconds=wait_seconds,
        )


class RulingStore:
    """Durable ruling storage plus a race-free condition-variable waiter."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.rulings_dir = data_dir / "rulings"
        self.rulings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._condition = threading.Condition()
        self._entries = self._load_entries()

    def _load_entries(self) -> list[dict[str, Any]]:
        by_sequence: dict[int, dict[str, Any]] = {}
        for path in sorted(self.rulings_dir.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                sequence = entry["sequence"]
                artifacts = entry["artifacts"]
                markdown_name = artifacts["markdown"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(f"cannot load persisted ruling {path}: {exc}") from exc
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence <= 0
            ):
                raise RuntimeError(f"invalid sequence in persisted ruling {path}")
            if sequence in by_sequence:
                raise RuntimeError(f"duplicate persisted ruling sequence {sequence}")
            if not isinstance(markdown_name, str):
                raise RuntimeError(f"invalid markdown artifact in persisted ruling {path}")
            markdown_path = self.rulings_dir / Path(markdown_name).name
            if not markdown_path.is_file():
                raise RuntimeError(
                    f"persisted ruling {path} is missing {markdown_path.name}"
                )
            by_sequence[sequence] = entry
        return [by_sequence[sequence] for sequence in sorted(by_sequence)]

    @property
    def latest_sequence(self) -> int:
        with self._condition:
            return self._entries[-1]["sequence"] if self._entries else 0

    @property
    def count(self) -> int:
        with self._condition:
            return len(self._entries)

    def _first_after(self, cursor: int) -> dict[str, Any] | None:
        for entry in self._entries:
            if entry["sequence"] > cursor:
                return entry
        return None

    def wait_after(self, cursor: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                entry = self._first_after(cursor)
                if entry is not None:
                    return entry
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def file(self, issue: str, markdown: str, form: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            sequence = self._entries[-1]["sequence"] + 1 if self._entries else 1
            basename = f"{sequence:020d}-{_safe_issue_slug(issue)}"
            markdown_name = f"{basename}.md"
            json_name = f"{basename}.json"
            entry: dict[str, Any] = {
                "sequence": sequence,
                "issue": issue,
                "submitted_at": _utc_now(),
                "markdown": markdown,
                "form": form,
                "artifacts": {
                    "markdown": markdown_name,
                    "json": json_name,
                },
            }
            markdown_bytes = (markdown.rstrip() + "\n").encode("utf-8")
            json_bytes = (
                json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            # The JSON file is the commit marker.  An interruption between the two
            # writes can leave an unreferenced Markdown file, never a half-ruling.
            _atomic_write(self.rulings_dir / markdown_name, markdown_bytes)
            _atomic_write(self.rulings_dir / json_name, json_bytes)
            self._entries.append(entry)
            self._condition.notify_all()
            return entry


class ArachneServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, store: RulingStore) -> None:
        self.config = config
        self.store = store
        super().__init__((LOOPBACK_HOST, config.port), ArachneHandler)


class ArachneHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Arachne/1"

    @property
    def arachne(self) -> ArachneServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)

    def _write(
        self,
        status: HTTPStatus,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        self._write(status, body)

    def _problem(self, problem: ClientProblem) -> None:
        self._json(
            problem.status,
            {
                "error": problem.title,
                "detail": problem.detail,
                "status": int(problem.status),
            },
        )

    def _dispatch(self, callback: Any) -> None:
        try:
            callback()
        except ClientProblem as problem:
            self._problem(problem)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("client disconnected before the response completed")
        except Exception as exc:  # pragma: no cover - exercised by fault injection
            LOGGER.error("unhandled request error:\n%s", traceback.format_exc())
            try:
                self._problem(
                    ClientProblem(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "internal_error",
                        f"{type(exc).__name__}: {exc}",
                    )
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(self._get)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(self._post)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._dispatch(self._head)

    def _head(self) -> None:
        raise ClientProblem(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "HEAD is not supported",
        )

    def _get(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "latest_sequence": self.arachne.store.latest_sequence,
                    "ruling_count": self.arachne.store.count,
                    "bound_host": LOOPBACK_HOST,
                    "port": self.arachne.server_port,
                },
            )
            return
        if path == "/wait":
            query = parse_qs(parsed.query, keep_blank_values=True)
            values = query.get("since")
            if values is None or len(values) != 1:
                raise ClientProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_cursor",
                    "provide exactly one non-negative integer 'since' cursor",
                )
            try:
                cursor = int(values[0])
            except ValueError as exc:
                raise ClientProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_cursor",
                    "'since' must be a non-negative integer",
                ) from exc
            if cursor < 0:
                raise ClientProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_cursor",
                    "'since' must be a non-negative integer",
                )
            entry = self.arachne.store.wait_after(
                cursor, self.arachne.config.wait_seconds
            )
            if entry is None:
                self._write(HTTPStatus.NO_CONTENT)
            else:
                self._json(HTTPStatus.OK, entry)
            return
        self._serve_page(path)

    def _serve_page(self, path: str) -> None:
        name = path.removeprefix("/")
        if not path.startswith("/") or not PAGE_NAME.fullmatch(name):
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such published decision page",
            )
        candidate = self.arachne.config.pages_dir / name
        # Exact, top-level, regular, non-symlink HTML files are the allowlist.
        if candidate.is_symlink() or not candidate.is_file():
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such published decision page",
            )
        body = candidate.read_bytes()
        self._write(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def _post(self) -> None:
        parsed = urlsplit(self.path)
        if unquote(parsed.path) != "/ruling" or parsed.query:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND, "not_found", "no such endpoint"
            )
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise ClientProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "POST /ruling requires Content-Type: application/json",
            )
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ClientProblem(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "Content-Length is required",
            )
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_length",
                "Content-Length must be an integer",
            ) from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ClientProblem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                f"ruling payload must be between 1 and {MAX_REQUEST_BYTES} bytes",
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST, "invalid_json", f"invalid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_ruling",
                "the request body must be a JSON object",
            )
        issue_raw = payload.get("issue")
        if isinstance(issue_raw, (str, int)) and not isinstance(issue_raw, bool):
            issue = str(issue_raw).strip()
        else:
            issue = ""
        markdown = payload.get("markdown")
        form = payload.get("form")
        if not issue or len(issue) > 200:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_ruling",
                "'issue' must be a non-empty string or integer (max 200 characters)",
            )
        if not isinstance(markdown, str) or not markdown.strip():
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_ruling",
                "'markdown' must be a non-empty string",
            )
        if not isinstance(form, dict):
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_ruling",
                "'form' must be a JSON object",
            )
        entry = self.arachne.store.file(issue, markdown, form)
        self._json(HTTPStatus.CREATED, entry)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ARACHNE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_environment()
    config.pages_dir.mkdir(parents=True, exist_ok=True)
    store = RulingStore(config.data_dir)
    server = ArachneServer(config, store)

    stop_requested = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %s; stopping", signum)
        stop_requested.set()
        # shutdown() must run outside serve_forever()'s thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    startup = {
        "event": "started",
        "host": LOOPBACK_HOST,
        "port": server.server_port,
        "pages_dir": str(config.pages_dir),
        "data_dir": str(config.data_dir),
        "latest_sequence": store.latest_sequence,
    }
    print(json.dumps(startup, sort_keys=True), flush=True)
    LOGGER.info("listening on http://%s:%s", LOOPBACK_HOST, server.server_port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0 if stop_requested.is_set() else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
