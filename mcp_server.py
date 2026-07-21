#!/usr/bin/env python3
"""A shared, authenticated Streamable HTTP MCP adapter for Arachne."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import ssl
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


AUTH_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Kept in step with the [project] version in pyproject.toml; the project is
# not an installed distribution (tool.uv package = false), so the version
# cannot be read from importlib metadata.
ADAPTER_VERSION = "0.2.0"


def _required_url(name: str, value: str | None, *, public: bool = False) -> str:
    if not value:
        raise ValueError(f"{name} is required")
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTP(S) origin without credentials")
    if public and parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"{name} must use HTTPS unless it is loopback-only")
    return normalized


def _positive_float(name: str, value: str, *, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not 0 < parsed <= maximum:
        raise ValueError(f"{name} must be greater than zero and at most {maximum}")
    return parsed


def _read_owner_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"cannot read MCP token {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise RuntimeError(
                f"MCP token must be an owner-only regular file: {path}"
            )
        try:
            with os.fdopen(descriptor, "r", encoding="ascii") as stream:
                descriptor = -1
                token = stream.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"cannot read MCP token {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if AUTH_TOKEN.fullmatch(token) is None:
        raise RuntimeError(f"MCP token is invalid: {path}")
    return token


@dataclass(frozen=True)
class Settings:
    """Portable MCP configuration supplied entirely outside the repository."""

    arachne_url: str
    public_url: str
    token_file: Path
    host: str
    port: int
    heartbeat_seconds: float
    request_timeout_seconds: float
    ca_file: Path | None
    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "Settings":
        state_root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
        token_file = Path(
            os.environ.get(
                "ARACHNE_TOKEN_FILE", state_root / "arachne" / "auth-token"
            )
        ).expanduser()
        host = os.environ.get("ARACHNE_MCP_HOST", "127.0.0.1")
        allow_remote = os.environ.get("ARACHNE_MCP_ALLOW_REMOTE", "false").lower()
        if host not in LOOPBACK_HOSTS and allow_remote not in {"1", "true", "yes", "on"}:
            raise ValueError(
                "ARACHNE_MCP_HOST must be loopback unless ARACHNE_MCP_ALLOW_REMOTE=true"
            )
        try:
            port = int(os.environ.get("ARACHNE_MCP_PORT", "8790"))
        except ValueError as exc:
            raise ValueError("ARACHNE_MCP_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("ARACHNE_MCP_PORT must be between 1 and 65535")
        allowed_hosts_text = os.environ.get("ARACHNE_MCP_ALLOWED_HOSTS", "")
        allowed_hosts = tuple(
            candidate.strip()
            for candidate in allowed_hosts_text.split(",")
            if candidate.strip()
        ) or (
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
        )
        ca_text = os.environ.get("ARACHNE_CA_FILE")
        return cls(
            arachne_url=_required_url("ARACHNE_URL", os.environ.get("ARACHNE_URL")),
            public_url=_required_url(
                "ARACHNE_PUBLIC_URL",
                os.environ.get("ARACHNE_PUBLIC_URL"),
                public=True,
            ),
            # Preserve the final path component so _read_owner_token can
            # reject a configured symlink instead of silently following it.
            token_file=token_file.absolute(),
            host=host,
            port=port,
            heartbeat_seconds=_positive_float(
                "ARACHNE_MCP_HEARTBEAT_SECONDS",
                os.environ.get("ARACHNE_MCP_HEARTBEAT_SECONDS", "30"),
                maximum=240,
            ),
            request_timeout_seconds=_positive_float(
                "ARACHNE_REQUEST_TIMEOUT",
                os.environ.get("ARACHNE_REQUEST_TIMEOUT", "570"),
                maximum=3600,
            ),
            ca_file=Path(ca_text).expanduser().resolve() if ca_text else None,
            allowed_hosts=allowed_hosts,
        )

    def token(self) -> str:
        return _read_owner_token(self.token_file)

    def ssl_context(self) -> ssl.SSLContext | bool:
        if self.ca_file is None:
            return True
        if not self.ca_file.is_file():
            raise RuntimeError(f"Arachne CA file is unavailable: {self.ca_file}")
        return ssl.create_default_context(cafile=self.ca_file)


class UpstreamProblem(RuntimeError):
    """A safe, credential-free description of an Arachne upstream failure."""


class ArachneClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self, *, wait: bool = False) -> httpx.AsyncClient:
        read_timeout = (
            self.settings.request_timeout_seconds if wait else 30.0
        )
        return httpx.AsyncClient(
            base_url=self.settings.arachne_url,
            headers={"Authorization": f"Bearer {self.settings.token()}"},
            verify=self.settings.ssl_context(),
            trust_env=False,
            timeout=httpx.Timeout(connect=10, read=read_timeout, write=30, pool=10),
        )

    @staticmethod
    def _safe_problem(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"upstream returned HTTP {response.status_code}"
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str) and detail:
            return f"upstream returned HTTP {response.status_code}: {detail}"
        return f"upstream returned HTTP {response.status_code}"

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=payload)
        except httpx.RequestError as exc:
            raise UpstreamProblem(
                f"Arachne is unreachable at {self.settings.arachne_url}"
            ) from exc
        if response.status_code not in expected:
            raise UpstreamProblem(self._safe_problem(response))
        try:
            result = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UpstreamProblem("Arachne returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise UpstreamProblem("Arachne returned a non-object JSON result")
        return result

    async def status(self, since: int) -> dict[str, Any]:
        health = await self._json_request("GET", "/health")
        backlog = await self._json_request("GET", f"/rulings?since={since}")
        return {"health": health, "backlog": backlog}

    async def get_ruling(self, sequence: int) -> dict[str, Any]:
        return await self._json_request("GET", f"/rulings/{sequence}")

    async def publish_decision(
        self,
        name: str,
        html: str,
        issue: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "html": html}
        if issue is not None:
            payload["issue"] = issue
        result = await self._json_request(
            "POST",
            "/pages",
            payload=payload,
            expected=(201,),
        )
        result["url"] = f"{self.settings.public_url}/{quote(name, safe='')}"
        return result

    async def bootstrap_url(self, page: str | None) -> dict[str, Any]:
        ticket = await self._json_request(
            "POST",
            "/bootstrap-ticket",
            payload={"page": page},
            expected=(201,),
        )
        candidate = ticket.get("ticket")
        if not isinstance(candidate, str) or AUTH_TOKEN.fullmatch(candidate) is None:
            raise UpstreamProblem("Arachne returned an invalid bootstrap ticket")
        destination = (
            "" if page is None else f"?next={quote(page, safe='')}"
        )
        return {
            "page": page,
            "expires_at": ticket.get("expires_at"),
            "url": (
                f"{self.settings.public_url}/bootstrap{destination}"
                f"#ticket={quote(candidate, safe='')}"
            ),
            "credential": "single-use bootstrap ticket",
        }

    async def wait_for_ruling(
        self, since: int, context: Context[Any, Any]
    ) -> dict[str, Any]:
        backoff = 1.0
        elapsed = 0.0
        async with self._client(wait=True) as client:
            while True:
                request = asyncio.create_task(client.get(f"/wait?since={since}"))
                while True:
                    try:
                        response = await asyncio.wait_for(
                            asyncio.shield(request),
                            timeout=self.settings.heartbeat_seconds,
                        )
                        break
                    except TimeoutError:
                        elapsed += self.settings.heartbeat_seconds
                        await context.report_progress(
                            elapsed,
                            message=f"Waiting for a ruling after sequence {since}",
                        )
                    except asyncio.CancelledError:
                        request.cancel()
                        raise
                    except httpx.RequestError:
                        break

                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
                    response = None
                else:
                    try:
                        response = request.result()
                    except httpx.RequestError:
                        response = None

                if response is None:
                    await context.report_progress(
                        elapsed,
                        message=f"Arachne unavailable; retrying in {backoff:g}s",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                if response.status_code == 204:
                    backoff = 1.0
                    continue
                if response.status_code != 200:
                    raise UpstreamProblem(self._safe_problem(response))
                try:
                    ruling = response.json()
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise UpstreamProblem("Arachne returned invalid ruling JSON") from exc
                sequence = ruling.get("sequence") if isinstance(ruling, dict) else None
                if (
                    not isinstance(sequence, int)
                    or isinstance(sequence, bool)
                    or sequence <= since
                ):
                    raise UpstreamProblem(
                        "Arachne returned a ruling without a valid advancing sequence"
                    )
                return {"cursor": sequence, "ruling": ruling}


class BearerAuthentication:
    """Pure ASGI bearer validation that does not disturb MCP streaming."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            authorization = next(
                (
                    value
                    for name, value in scope.get("headers", [])
                    if name.lower() == b"authorization"
                ),
                b"",
            )
            if not hmac.compare_digest(authorization, self.expected):
                body = b'{"error":"bearer_authentication_required"}\n'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"cache-control", b"no-store"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _positive(name: str, value: int) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def create_app(settings: Settings) -> Starlette:
    client = ArachneClient(settings)
    annotations_read = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    annotations_write = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    mcp = FastMCP(
        "Arachne",
        instructions=(
            "Arachne carries human rulings into the active agent session. "
            "Always pass the last observed sequence as 'since'; retain the returned "
            "cursor. Publication accepts trusted decision HTML only."
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=False,
        json_response=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=[],
        ),
    )
    # mcp 1.28.1 exposes no FastMCP version parameter. Left unset, the SDK
    # infers server_version from installed-package metadata at initialize
    # time, and importlib.metadata.version() can return None there (absent or
    # unreadable METADATA) — failing InitializationOptions validation and
    # crashing every handshake before a response is written.
    mcp._mcp_server.version = ADAPTER_VERSION

    @mcp.tool(annotations=annotations_read, structured_output=True)
    async def status(since: int = 0) -> dict[str, Any]:
        """Return service health and non-destructive ruling summaries after since."""

        _non_negative("since", since)
        return await client.status(since)

    @mcp.tool(annotations=annotations_read, structured_output=True)
    async def get_ruling(sequence: int) -> dict[str, Any]:
        """Peek at one complete persisted ruling without changing any cursor."""

        _positive("sequence", sequence)
        return await client.get_ruling(sequence)

    @mcp.tool(annotations=annotations_read, structured_output=True)
    async def wait_for_ruling(
        since: int, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Wait for the first ruling after since, with replay-safe progress heartbeats."""

        _non_negative("since", since)
        return await client.wait_for_ruling(since, ctx)

    @mcp.tool(annotations=annotations_write, structured_output=True)
    async def publish_decision(
        name: str,
        html: str,
        issue: str | None = None,
    ) -> dict[str, Any]:
        """Validate and atomically publish trusted decision HTML on Arachne.

        Pass the issue token the brief reports, or omit it to infer the token
        from a conventional ``decision_<issue>_*.html`` name.
        """

        return await client.publish_decision(name, html, issue)

    @mcp.tool(annotations=annotations_write, structured_output=True)
    async def bootstrap_url(page: str | None = None) -> dict[str, Any]:
        """Create a five-minute, single-use browser bootstrap URL.

        With a page name the session lands on that page; with no page it
        lands on the inbox at /, where every brief is reachable.
        """

        return await client.bootstrap_url(page)

    mcp_http = BearerAuthentication(mcp.streamable_http_app(), settings.token())

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "arachne-mcp"})

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Mount("/", app=mcp_http),
        ],
        lifespan=lifespan,
    )
    app.state.arachne_mcp = mcp
    return app


def main() -> int:
    settings = Settings.from_environment()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=os.environ.get("ARACHNE_MCP_LOG_LEVEL", "info").lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
