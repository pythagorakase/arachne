#!/usr/bin/env python3
"""Arachne's loopback-only decision server.

The service deliberately uses only the Python standard library.  Tailscale
Serve owns remote reachability and device authentication; an application
secret also protects the loopback listener from other users on a shared host.
This process never listens on a non-loopback interface.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import ssl
import stat
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from page_contract import PAGE_NAME, publish_html, read_page_issue
from ui import (
    BOOTSTRAP_CSP,
    INBOX_CSP,
    fallback_title,
    font_asset,
    page_title,
    public_app_asset,
    render_bootstrap,
    render_inbox,
    render_locked_inbox,
)


LOGGER = logging.getLogger("arachne")
LOOPBACK_HOST = "127.0.0.1"
MAX_REQUEST_BYTES = 1_048_576
SESSION_COOKIE_NAME = "arachne_session"
SESSION_COOKIE_SECONDS = 15 * 24 * 60 * 60
# A session presented past its half-life is re-issued for the full window, so a
# regularly used device never re-enrolls while an idle one still ages out.
SESSION_RENEWAL_SECONDS = SESSION_COOKIE_SECONDS // 2
SESSION_COOKIE_VERSION = "v1"
BOOTSTRAP_TICKET_SECONDS = 5 * 60
# The inbox is addressed by "/" everywhere a page name may appear: as a
# bootstrap-ticket binding, as a session destination, and in the router.
INBOX_PATH = "/"
TLS_HANDSHAKE_TIMEOUT = 5
MAX_CONNECTION_WORKERS = 32
ISSUE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
AUTH_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
BOOTSTRAP_TICKET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
SESSION_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
DECISION_PAGE_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' data: blob:; "
    "connect-src 'self'; "
    "worker-src blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-src 'none'; "
    "frame-ancestors 'self'"
)


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
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
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


def _atomic_create(path: Path, data: bytes) -> None:
    """Install a complete owner-only file without replacing a concurrent winner."""

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.create"
    )
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return
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


def _environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def _page_issue(name: str) -> str:
    """Derive the issue token a page's rulings will carry.

    ``decision_<issue>_<slug>.html`` and ``decision_<issue>.html`` yield
    ``<issue>``; any other allowlisted name yields its stem, matching pages
    (like the phone smoke check) whose scripts file the stem as their issue.
    """

    stem = name[: -len(".html")]
    if stem.startswith("decision_"):
        token = stem.removeprefix("decision_").split("_", 1)[0]
        if token:
            return token
    return stem


def _parse_submitted_at(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class Config:
    pages_dir: Path
    data_dir: Path
    token_file: Path
    port: int
    wait_seconds: float
    secure_cookie: bool
    tls_cert_file: Path | None
    tls_key_file: Path | None

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
        token_file = Path(
            os.environ.get("ARACHNE_TOKEN_FILE", data_dir / "auth-token")
        ).expanduser()
        port_text = os.environ.get("ARACHNE_PORT", os.environ.get("BEAN_PORT", "8788"))
        wait_text = os.environ.get("ARACHNE_WAIT_SECONDS", "540")
        tls_cert_text = os.environ.get("ARACHNE_TLS_CERT_FILE")
        tls_key_text = os.environ.get("ARACHNE_TLS_KEY_FILE")
        if (tls_cert_text is None) != (tls_key_text is None):
            raise ValueError(
                "ARACHNE_TLS_CERT_FILE and ARACHNE_TLS_KEY_FILE must be set together"
            )
        if tls_cert_text is not None and (
            not tls_cert_text.strip() or not tls_key_text or not tls_key_text.strip()
        ):
            raise ValueError(
                "ARACHNE_TLS_CERT_FILE and ARACHNE_TLS_KEY_FILE must be non-empty"
            )
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
            # Keep the final path component unresolved so Authentication can
            # reject a token-file symlink before reading it.
            token_file=token_file.absolute(),
            port=port,
            wait_seconds=wait_seconds,
            secure_cookie=_environment_bool("ARACHNE_SECURE_COOKIE", True),
            tls_cert_file=(
                Path(tls_cert_text).expanduser().resolve()
                if tls_cert_text is not None
                else None
            ),
            tls_key_file=(
                Path(tls_key_text).expanduser().resolve()
                if tls_key_text is not None
                else None
            ),
        )


class Authentication:
    """Owner-only bearer token and expiring browser-session credentials."""

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(token_file.parent, 0o700)
        if token_file.is_symlink():
            raise RuntimeError(f"authentication token must not be a symlink: {token_file}")
        if not token_file.exists():
            token = secrets.token_urlsafe(32)
            _atomic_create(token_file, f"{token}\n".encode("ascii"))
        metadata = token_file.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"authentication token is not a regular file: {token_file}")
        os.chmod(token_file, 0o600)
        try:
            token = token_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"cannot read authentication token {token_file}: {exc}") from exc
        if not AUTH_TOKEN.fullmatch(token):
            raise RuntimeError(
                f"authentication token in {token_file} must be 32-256 URL-safe characters"
            )
        self._token = token
        self._session_key = token.encode("ascii")
        self._ticket_lock = threading.Lock()
        self._bootstrap_tickets: dict[str, tuple[str, int]] = {}

    def _session_signature(self, expires_at: int) -> str:
        message = f"arachne-browser-session-v1:{expires_at}".encode("ascii")
        return hmac.new(self._session_key, message, hashlib.sha256).hexdigest()

    def _session_value(self, expires_at: int) -> str:
        return (
            f"{SESSION_COOKIE_VERSION}.{expires_at}."
            f"{self._session_signature(expires_at)}"
        )

    def accepts_token(self, candidate: object) -> bool:
        return isinstance(candidate, str) and hmac.compare_digest(candidate, self._token)

    def accepts_bearer(self, authorization: str | None) -> bool:
        if not authorization or not authorization.startswith("Bearer "):
            return False
        return self.accepts_token(authorization.removeprefix("Bearer "))

    def cookie_expiry(self, cookie_header: str | None) -> int | None:
        """Return the verified expiry of a live session cookie, else None."""

        if not cookie_header:
            return None
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except CookieError:
            return None
        morsel = cookies.get(SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        try:
            version, expires_text, candidate_signature = morsel.value.split(".", 2)
            if version != SESSION_COOKIE_VERSION or not expires_text.isascii():
                return None
            if not expires_text.isdecimal() or str(int(expires_text)) != expires_text:
                return None
            expires_at = int(expires_text)
        except (TypeError, ValueError):
            return None
        expected_signature = self._session_signature(expires_at)
        if not SESSION_SIGNATURE.fullmatch(candidate_signature):
            return None
        if not hmac.compare_digest(candidate_signature, expected_signature):
            return None
        if expires_at <= int(time.time()):
            return None
        return expires_at

    def accepts_cookie(self, cookie_header: str | None) -> bool:
        return self.cookie_expiry(cookie_header) is not None

    def issue_bootstrap_ticket(self, page: str) -> tuple[str, int]:
        """Create a short-lived, single-use browser bootstrap capability."""

        now = int(time.time())
        expires_at = now + BOOTSTRAP_TICKET_SECONDS
        ticket = secrets.token_urlsafe(32)
        with self._ticket_lock:
            self._bootstrap_tickets = {
                candidate: record
                for candidate, record in self._bootstrap_tickets.items()
                if record[1] > now
            }
            self._bootstrap_tickets[ticket] = (page, expires_at)
        return ticket, expires_at

    def consume_bootstrap_ticket(self, candidate: object, page: object) -> bool:
        """Consume a valid ticket exactly once and only for its bound page."""

        if (
            not isinstance(candidate, str)
            or BOOTSTRAP_TICKET.fullmatch(candidate) is None
            or not isinstance(page, str)
        ):
            return False
        now = int(time.time())
        with self._ticket_lock:
            record = self._bootstrap_tickets.pop(candidate, None)
        if record is None:
            return False
        expected_page, expires_at = record
        return expires_at > now and hmac.compare_digest(expected_page, page)

    def session_cookie(self, secure: bool) -> str:
        expires_at = int(time.time()) + SESSION_COOKIE_SECONDS
        attributes = [
            f"{SESSION_COOKIE_NAME}={self._session_value(expires_at)}",
            "Path=/",
            f"Max-Age={SESSION_COOKIE_SECONDS}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if secure:
            attributes.append("Secure")
        return "; ".join(attributes)


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
                issue = entry["issue"]
                submitted_at = entry["submitted_at"]
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
            if not isinstance(issue, str) or not issue:
                raise RuntimeError(f"invalid issue in persisted ruling {path}")
            if not isinstance(submitted_at, str) or not submitted_at:
                raise RuntimeError(f"invalid submission time in persisted ruling {path}")
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

    def summaries_after(self, cursor: int) -> tuple[int, list[dict[str, Any]]]:
        """Return an atomic, read-only snapshot of rulings after *cursor*."""

        with self._condition:
            latest_sequence = self._entries[-1]["sequence"] if self._entries else 0
            summaries = [
                {
                    "sequence": entry["sequence"],
                    "issue": entry["issue"],
                    "submitted_at": entry["submitted_at"],
                }
                for entry in self._entries
                if entry["sequence"] > cursor
            ]
        return latest_sequence, summaries

    def get(self, sequence: int) -> dict[str, Any] | None:
        """Return a ruling without changing any waiter or client cursor state."""

        with self._condition:
            for entry in self._entries:
                if entry["sequence"] == sequence:
                    return dict(entry)
        return None

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

    def __init__(
        self, config: Config, store: RulingStore, authentication: Authentication
    ) -> None:
        self.config = config
        self.store = store
        self.authentication = authentication
        if (config.tls_cert_file is None) != (config.tls_key_file is None):
            raise RuntimeError("TLS certificate and private key must be configured together")
        tls_context: ssl.SSLContext | None = None
        if config.tls_cert_file is not None:
            assert config.tls_key_file is not None
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.load_cert_chain(config.tls_cert_file, config.tls_key_file)
        self._tls_context = tls_context
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTION_WORKERS)
        super().__init__((LOOPBACK_HOST, config.port), ArachneHandler)
        self.tls_enabled = tls_context is not None

    def process_request(self, request: Any, client_address: Any) -> None:
        """Bound unauthenticated connection workers before creating a thread."""

        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Perform each TLS handshake after the accept loop dispatches a worker."""

        try:
            if self._tls_context is None:
                super().process_request_thread(request, client_address)
                return

            tls_request: ssl.SSLSocket | None = None
            try:
                request.settimeout(TLS_HANDSHAKE_TIMEOUT)
                tls_request = self._tls_context.wrap_socket(
                    request,
                    server_side=True,
                    do_handshake_on_connect=False,
                )
                tls_request.do_handshake()
            except Exception as exc:
                LOGGER.debug("TLS handshake failed for %s: %s", client_address[0], exc)
                self.shutdown_request(
                    tls_request if tls_request is not None else request
                )
                return

            # StreamRequestHandler.setup() replaces the handshake timeout with the
            # handler's normal request timeout before parsing HTTP.
            super().process_request_thread(tls_request, client_address)
        finally:
            self._connection_slots.release()


class ArachneHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Arachne/1"
    timeout = 75

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
        *,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        if status != HTTPStatus.NO_CONTENT:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        renewed_cookie = getattr(self, "_renewed_session_cookie", None)
        if renewed_cookie is not None:
            self.send_header("Set-Cookie", renewed_cookie)
        self.end_headers()
        if body and not head_only:
            self.wfile.write(body)

    def _json(
        self,
        status: HTTPStatus,
        payload: Any,
        *,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        self._write(
            status, body, extra_headers=extra_headers, head_only=head_only
        )

    def _problem(self, problem: ClientProblem) -> None:
        self._json(
            problem.status,
            {
                "error": problem.title,
                "detail": problem.detail,
                "status": int(problem.status),
            },
            head_only=self.command == "HEAD",
        )

    def _dispatch(self, callback: Any) -> None:
        try:
            callback()
        except ClientProblem as problem:
            self._close_incomplete_post()
            self._problem(problem)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.info("client disconnected before the response completed")
        except Exception as exc:  # pragma: no cover - exercised by fault injection
            self._close_incomplete_post()
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

    def _close_incomplete_post(self) -> None:
        if self.command == "POST" and not getattr(
            self, "_post_body_consumed", False
        ):
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self._renewed_session_cookie: str | None = None
        self._dispatch_bodyless(self._get)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._renewed_session_cookie = None
        self._post_body_consumed = False
        self._dispatch(self._post)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        self._renewed_session_cookie = None
        self._dispatch_bodyless(self._head)

    def _dispatch_bodyless(self, callback: Any) -> None:
        def guarded_callback() -> None:
            transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
            length_headers = self.headers.get_all("Content-Length", [])
            invalid_length = (
                len(length_headers) > 1
                or (
                    len(length_headers) == 1
                    and length_headers[0] != "0"
                )
            )
            if transfer_encoding or invalid_length:
                self.close_connection = True
                raise ClientProblem(
                    HTTPStatus.BAD_REQUEST,
                    "unexpected_request_body",
                    f"{self.command} requests must not carry a body",
                )
            callback()

        self._dispatch(guarded_callback)

    def _head(self) -> None:
        parsed = urlsplit(self.path)
        if unquote(parsed.path) == "/health":
            self._json(HTTPStatus.OK, self._health_payload(), head_only=True)
            return
        raise ClientProblem(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "HEAD is supported only for /health",
        )

    def _health_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "latest_sequence": self.arachne.store.latest_sequence,
            "ruling_count": self.arachne.store.count,
            "bound_host": LOOPBACK_HOST,
            "port": self.arachne.server_port,
            "tls": self.arachne.tls_enabled,
        }

    def _require_authentication(self) -> None:
        authentication = self.arachne.authentication
        if authentication.accepts_bearer(self.headers.get("Authorization")):
            return
        expires_at = authentication.cookie_expiry(self.headers.get("Cookie"))
        if expires_at is not None:
            if expires_at - int(time.time()) < SESSION_RENEWAL_SECONDS:
                self._renewed_session_cookie = authentication.session_cookie(
                    self.arachne.config.secure_cookie
                )
            return
        raise ClientProblem(
            HTTPStatus.UNAUTHORIZED,
            "authentication_required",
            "provide a valid Arachne browser session or bearer token",
        )

    def _require_bearer_authentication(self) -> None:
        if self.arachne.authentication.accepts_bearer(
            self.headers.get("Authorization")
        ):
            return
        raise ClientProblem(
            HTTPStatus.UNAUTHORIZED,
            "bearer_authentication_required",
            "provide the owner Arachne bearer token",
        )

    def _parse_cursor(self, raw_query: str) -> int:
        query = parse_qs(raw_query, keep_blank_values=True)
        if set(query) != {"since"} or len(query["since"]) != 1:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_cursor",
                "provide exactly one non-negative integer 'since' cursor",
            )
        value = query["since"][0]
        if re.fullmatch(r"[0-9]+", value) is None:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_cursor",
                "'since' must be a non-negative integer",
            )
        try:
            return int(value)
        except ValueError as exc:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_cursor",
                "'since' must be a non-negative integer",
            ) from exc

    def _get(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        install_asset = public_app_asset(path)
        if install_asset is not None:
            if parsed.query:
                raise ClientProblem(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "no such Arachne install asset",
                )
            body, content_type = install_asset
            self._write(HTTPStatus.OK, body, content_type)
            return
        if path == "/health":
            self._json(HTTPStatus.OK, self._health_payload())
            return
        if path == "/bootstrap":
            self._serve_bootstrap(parsed.query)
            return
        if path == "/wait":
            self._require_authentication()
            cursor = self._parse_cursor(parsed.query)
            entry = self.arachne.store.wait_after(
                cursor, self.arachne.config.wait_seconds
            )
            if entry is None:
                self._write(HTTPStatus.NO_CONTENT)
            else:
                self._json(HTTPStatus.OK, entry)
            return
        if path == "/rulings":
            self._require_authentication()
            cursor = self._parse_cursor(parsed.query)
            latest_sequence, summaries = self.arachne.store.summaries_after(cursor)
            self._json(
                HTTPStatus.OK,
                {
                    "since": cursor,
                    "latest_sequence": latest_sequence,
                    "rulings": summaries,
                },
            )
            return
        if path.startswith("/rulings/"):
            self._require_authentication()
            self._serve_ruling(path, parsed.query)
            return
        if path == INBOX_PATH and not parsed.query:
            self._serve_inbox()
            return
        self._require_authentication()
        if path.startswith("/ui/fonts/"):
            self._serve_font(path, parsed.query)
            return
        self._serve_page(path)

    def _serve_ruling(self, path: str, raw_query: str) -> None:
        sequence_text = path.removeprefix("/rulings/")
        if raw_query or re.fullmatch(r"[1-9][0-9]*", sequence_text) is None:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such persisted ruling",
            )
        try:
            sequence = int(sequence_text)
        except ValueError as exc:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such persisted ruling",
            ) from exc
        entry = self.arachne.store.get(sequence)
        if entry is None:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such persisted ruling",
            )
        self._json(HTTPStatus.OK, entry)

    def _page_candidate(self, name: str) -> Path:
        if not PAGE_NAME.fullmatch(name):
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such published decision page",
            )
        candidate = self.arachne.config.pages_dir / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such published decision page",
            )
        return candidate

    def _serve_bootstrap(self, raw_query: str) -> None:
        if raw_query:
            query = parse_qs(raw_query, keep_blank_values=True)
            if set(query) != {"next"} or len(query["next"]) != 1:
                raise ClientProblem(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_bootstrap",
                    "provide exactly one allowlisted 'next' page, or none for the inbox",
                )
            name = query["next"][0].removeprefix("/")
            self._page_candidate(name)
            binding = name
            destination = f"/{name}"
        else:
            # No destination page: establish the session and land on the inbox.
            binding = INBOX_PATH
            destination = INBOX_PATH
        body = render_bootstrap(binding, destination)
        self._write(
            HTTPStatus.OK,
            body,
            "text/html; charset=utf-8",
            extra_headers={"Content-Security-Policy": BOOTSTRAP_CSP},
        )

    def _serve_page(self, path: str) -> None:
        name = path.removeprefix("/")
        if not path.startswith("/"):
            raise ClientProblem(HTTPStatus.NOT_FOUND, "not_found", "no such page")
        # Exact, top-level, regular, non-symlink HTML files are the allowlist.
        candidate = self._page_candidate(name)
        body = candidate.read_bytes()
        self._write(
            HTTPStatus.OK,
            body,
            "text/html; charset=utf-8",
            extra_headers={"Content-Security-Policy": DECISION_PAGE_CSP},
        )

    def _serve_font(self, path: str, raw_query: str) -> None:
        """Serve one application-owned font from the fixed UI allowlist."""

        name = path.removeprefix("/ui/fonts/")
        if raw_query or not name or "/" in name:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such Arachne UI font",
            )
        body = font_asset(name)
        if body is None:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "no such Arachne UI font",
            )
        self._write(HTTPStatus.OK, body, "font/ttf")

    def _serve_inbox(self) -> None:
        """Render the stable mailbox: authenticated state only, no secrets.

        An unauthenticated visit (a bookmark whose session lapsed) receives a
        friendly 401 shell that names no pages, no rulings, and no counts.
        """

        try:
            self._require_authentication()
        except ClientProblem:
            self._write(
                HTTPStatus.UNAUTHORIZED,
                render_locked_inbox(),
                "text/html; charset=utf-8",
                extra_headers={"Content-Security-Policy": INBOX_CSP},
            )
            return
        pending, archived = self._inbox_entries()
        self._write(
            HTTPStatus.OK,
            render_inbox(pending, archived),
            "text/html; charset=utf-8",
            extra_headers={"Content-Security-Policy": INBOX_CSP},
        )

    def _inbox_entries(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split allowlisted pages into pending and archived, both derived.

        A page is archived when a ruling carrying its issue token was filed at
        or after the page's publication time; filing a ruling therefore *is*
        the archive action, and re-publishing a page for the same issue
        returns it to pending. No mutable inbox state exists to maintain.
        """

        _, summaries = self.arachne.store.summaries_after(0)
        rulings_by_issue: dict[str, list[tuple[float, int]]] = {}
        for summary in summaries:
            moment = _parse_submitted_at(summary.get("submitted_at"))
            if moment is None:
                continue
            rulings_by_issue.setdefault(str(summary["issue"]), []).append(
                (moment, summary["sequence"])
            )
        pending: list[dict[str, Any]] = []
        archived: list[dict[str, Any]] = []
        pages_dir = self.arachne.config.pages_dir
        try:
            names = sorted(entry.name for entry in pages_dir.iterdir())
        except OSError:
            names = []
        for name in names:
            if PAGE_NAME.fullmatch(name) is None:
                continue
            candidate = pages_dir / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            # Floor the mtime to whole milliseconds: submitted_at is persisted
            # at millisecond precision, so comparing against a finer-grained
            # mtime would let a ruling filed later in the same millisecond
            # appear to precede its page's publication and never archive it.
            published_at = int(candidate.stat().st_mtime * 1000) / 1000
            # The issue recorded at publication is authoritative; filename
            # inference remains only as a fallback for pre-metadata pages.
            issue = read_page_issue(pages_dir, name) or _page_issue(name)
            entry = {
                "name": name,
                "issue": issue,
                "title": page_title(candidate) or fallback_title(name, issue),
                "published_at": published_at,
            }
            filed = [
                (moment, sequence)
                for moment, sequence in rulings_by_issue.get(issue, [])
                if moment >= published_at
            ]
            if filed:
                ruled_at, ruling_sequence = max(
                    filed, key=lambda record: record[1]
                )
                entry["ruled_at"] = ruled_at
                entry["ruling_sequence"] = ruling_sequence
                archived.append(entry)
            else:
                pending.append(entry)
        pending.sort(key=lambda entry: entry["published_at"], reverse=True)
        archived.sort(key=lambda entry: entry["ruling_sequence"], reverse=True)
        return pending, archived

    def _post(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/session" and not parsed.query:
            self._establish_session()
            return
        if path == "/bootstrap-ticket" and not parsed.query:
            self._require_bearer_authentication()
            self._issue_bootstrap_ticket()
            return
        if path == "/pages" and not parsed.query:
            self._require_bearer_authentication()
            self._publish_page()
            return
        if path != "/ruling" or parsed.query:
            raise ClientProblem(
                HTTPStatus.NOT_FOUND, "not_found", "no such endpoint"
            )
        self._require_authentication()
        payload = self._read_json_payload("POST /ruling", "ruling")
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
        acknowledgement = dict(entry)
        # Existing NEXUS decision pages predate Arachne and treat these two
        # fields as the success contract.  Keep the durable entry fields too,
        # so newer clients can consume the sequence and artifact metadata.
        acknowledgement.update(
            {
                "ok": True,
                "filed": entry["artifacts"]["markdown"],
            }
        )
        self._json(HTTPStatus.CREATED, acknowledgement)

    def _issue_bootstrap_ticket(self) -> None:
        payload = self._read_json_payload(
            "POST /bootstrap-ticket", "bootstrap ticket"
        )
        if not isinstance(payload, dict) or not set(payload) <= {"page"}:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_bootstrap_ticket",
                "the bootstrap ticket request must contain only an optional page",
            )
        page = payload.get("page")
        if page is None:
            binding = INBOX_PATH
        elif isinstance(page, str):
            self._page_candidate(page)
            binding = page
        else:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_bootstrap_ticket",
                "'page' must name a published decision page, or be null for the inbox",
            )
        ticket, expires_at = self.arachne.authentication.issue_bootstrap_ticket(
            binding
        )
        self._json(
            HTTPStatus.CREATED,
            {
                "ticket": ticket,
                "expires_at": expires_at,
                "page": None if binding == INBOX_PATH else binding,
            },
        )

    def _publish_page(self) -> None:
        payload = self._read_json_payload("POST /pages", "decision page")
        if (
            not isinstance(payload, dict)
            or not {"name", "html"} <= set(payload)
            or not set(payload) <= {"name", "html", "issue"}
        ):
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_page",
                (
                    "the publication request must contain name and html, "
                    "and may optionally contain issue"
                ),
            )
        name = payload["name"]
        html = payload["html"]
        if not isinstance(name, str) or not isinstance(html, str):
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_page",
                "'name' and 'html' must be strings",
            )
        try:
            publication = publish_html(
                name,
                html,
                self.arachne.config.pages_dir,
                issue=payload.get("issue"),
            )
        except ValueError as exc:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_page",
                str(exc),
            ) from exc
        self._json(
            HTTPStatus.CREATED,
            {
                "ok": True,
                "page": publication.name,
                "issue": publication.issue,
            },
        )

    def _read_json_payload(self, endpoint: str, noun: str) -> Any:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "unsupported_transfer_encoding",
                (
                    f"{endpoint} requires an exact Content-Length and does not "
                    "accept Transfer-Encoding"
                ),
            )
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise ClientProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                f"{endpoint} requires Content-Type: application/json",
            )
        length_headers = self.headers.get_all("Content-Length", [])
        if not length_headers:
            raise ClientProblem(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "Content-Length is required",
            )
        if len(length_headers) != 1 or not length_headers[0].isdecimal():
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_length",
                "Content-Length must be exactly one decimal integer",
            )
        length_text = length_headers[0]
        if len(length_text) > len(str(MAX_REQUEST_BYTES)):
            raise ClientProblem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                f"{noun} payload must be between 1 and {MAX_REQUEST_BYTES} bytes",
            )
        length = int(length_text)
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ClientProblem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                f"{noun} payload must be between 1 and {MAX_REQUEST_BYTES} bytes",
            )
        try:
            raw = self.rfile.read(length)
        except TimeoutError as exc:
            raise ClientProblem(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                f"timed out while reading the {noun} payload",
            ) from exc
        if len(raw) != length:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "incomplete_body",
                f"expected {length} payload bytes but received {len(raw)}",
            )
        self._post_body_consumed = True
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST, "invalid_json", f"invalid JSON: {exc}"
            ) from exc
        return payload

    def _establish_session(self) -> None:
        payload = self._read_json_payload("POST /session", "session")
        if not isinstance(payload, dict):
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_session",
                "the session request must contain a token or one-time ticket",
            )
        if set(payload) == {"token"}:
            accepted = self.arachne.authentication.accepts_token(payload["token"])
        elif set(payload) == {"ticket", "page"}:
            page = payload["page"]
            if not isinstance(page, str):
                accepted = False
            else:
                if page != INBOX_PATH:
                    self._page_candidate(page)
                accepted = self.arachne.authentication.consume_bootstrap_ticket(
                    payload["ticket"], page
                )
        else:
            raise ClientProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_session",
                "the session request must contain only token, or ticket and page",
            )
        if not accepted:
            raise ClientProblem(
                HTTPStatus.UNAUTHORIZED,
                "invalid_credential",
                "the Arachne bootstrap credential is invalid or expired",
            )
        self._write(
            HTTPStatus.NO_CONTENT,
            extra_headers={
                "Set-Cookie": self.arachne.authentication.session_cookie(
                    self.arachne.config.secure_cookie
                )
            },
        )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ARACHNE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_environment()
    config.pages_dir.mkdir(parents=True, exist_ok=True)
    authentication = Authentication(config.token_file)
    store = RulingStore(config.data_dir)
    server = ArachneServer(config, store, authentication)

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
        "token_file": str(config.token_file),
        "latest_sequence": store.latest_sequence,
        "tls": server.tls_enabled,
    }
    print(json.dumps(startup, sort_keys=True), flush=True)
    scheme = "https" if server.tls_enabled else "http"
    LOGGER.info("listening on %s://%s:%s", scheme, LOOPBACK_HOST, server.server_port)
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
