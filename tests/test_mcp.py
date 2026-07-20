from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.request import urlopen

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_server import ADAPTER_VERSION, _read_owner_token
from tests.test_e2e import RunningArachne, bearer, free_port, post_ruling


REPO = Path(__file__).resolve().parents[1]


class RunningMCP:
    def __init__(self, arachne: RunningArachne) -> None:
        self.arachne = arachne
        self.port = free_port()
        self.process: subprocess.Popen[str] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp"

    def start(self) -> None:
        environment = os.environ.copy()
        environment.update(
            {
                "ARACHNE_URL": self.arachne.url,
                "ARACHNE_PUBLIC_URL": "https://arachne.example.test",
                "ARACHNE_TOKEN_FILE": str(self.arachne.token_file),
                "ARACHNE_MCP_HOST": "127.0.0.1",
                "ARACHNE_MCP_PORT": str(self.port),
                "ARACHNE_MCP_HEARTBEAT_SECONDS": "0.05",
                "ARACHNE_REQUEST_TIMEOUT": "2",
                "ARACHNE_MCP_LOG_LEVEL": "warning",
            }
        )
        self.process = subprocess.Popen(
            [sys.executable, str(REPO / "mcp_server.py")],
            cwd=REPO,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                raise AssertionError(
                    f"MCP server exited during startup\nstdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                with urlopen(f"{self.url}/health", timeout=0.2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.02)
        raise AssertionError("MCP server did not become healthy")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process.communicate()
        self.process = None


class MCPTokenTests(unittest.TestCase):
    def test_owner_only_regular_token_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "auth-token"
            token_file.write_text("a" * 32 + "\n", encoding="ascii")
            token_file.chmod(0o600)
            self.assertEqual(_read_owner_token(token_file), "a" * 32)

    def test_group_readable_token_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_file = Path(temporary) / "auth-token"
            token_file.write_text("a" * 32 + "\n", encoding="ascii")
            token_file.chmod(0o640)
            with self.assertRaisesRegex(RuntimeError, "owner-only regular file"):
                _read_owner_token(token_file)

    def test_token_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "real-token"
            target.write_text("a" * 32 + "\n", encoding="ascii")
            target.chmod(0o600)
            link = root / "auth-token"
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "cannot read MCP token"):
                _read_owner_token(link)


class AdapterVersionSyncTests(unittest.TestCase):
    def test_adapter_version_matches_the_project_version(self) -> None:
        with (REPO / "pyproject.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        self.assertEqual(ADAPTER_VERSION, manifest["project"]["version"])


class ArachneMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.pages = root / "pages"
        self.data = root / "data"
        self.pages.mkdir()
        self.arachne = RunningArachne(self.pages, self.data, wait_seconds=0.5)
        self.arachne.start()
        self.mcp = RunningMCP(self.arachne)
        self.mcp.start()

    def tearDown(self) -> None:
        self.mcp.stop()
        self.arachne.stop()
        self.temporary.cleanup()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        async with httpx.AsyncClient(headers=bearer(self.arachne.token)) as client:
            async with streamable_http_client(
                self.mcp.mcp_url, http_client=client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session

    def structured(self, result: object) -> dict:
        self.assertFalse(getattr(result, "isError"), getattr(result, "content"))
        structured = getattr(result, "structuredContent")
        self.assertIsInstance(structured, dict)
        return structured

    async def test_initialize_advertises_the_adapter_version(self) -> None:
        async with httpx.AsyncClient(headers=bearer(self.arachne.token)) as client:
            async with streamable_http_client(
                self.mcp.mcp_url, http_client=client
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    result = await session.initialize()
        self.assertEqual(result.serverInfo.name, "Arachne")
        self.assertEqual(result.serverInfo.version, ADAPTER_VERSION)

    async def test_endpoint_authentication_tools_and_publication(self) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.post(self.mcp.mcp_url, json={})
        self.assertEqual(response.status_code, 401)

        async with self.session() as session:
            tools = await session.list_tools()
            self.assertEqual(
                {tool.name for tool in tools.tools},
                {
                    "bootstrap_url",
                    "get_ruling",
                    "publish_decision",
                    "status",
                    "wait_for_ruling",
                },
            )
            status = self.structured(
                await session.call_tool("status", arguments={"since": 0})
            )
            self.assertTrue(status["health"]["ok"])
            self.assertEqual(status["backlog"]["rulings"], [])

            html = """<!doctype html><script>
            localStorage.setItem('draft', 'yes');
            fetch('http://localhost:8788/ruling', {method: 'POST'});
            </script>"""
            published = self.structured(
                await session.call_tool(
                    "publish_decision",
                    arguments={
                        "name": "decision_mcp.html",
                        "html": html,
                        "issue": "mcp-476",
                    },
                )
            )
            self.assertEqual(published["page"], "decision_mcp.html")
            self.assertEqual(published["issue"], "mcp-476")
            self.assertEqual(
                published["url"],
                "https://arachne.example.test/decision_mcp.html",
            )
            self.assertIn("fetch('/ruling'", (self.pages / "decision_mcp.html").read_text())

            bootstrap = self.structured(
                await session.call_tool(
                    "bootstrap_url", arguments={"page": "decision_mcp.html"}
                )
            )
            self.assertIn("#ticket=", bootstrap["url"])
            self.assertNotIn(self.arachne.token, json.dumps(bootstrap))
            self.assertEqual(bootstrap["credential"], "single-use bootstrap ticket")

            inbox = self.structured(
                await session.call_tool("bootstrap_url", arguments={})
            )
            self.assertIsNone(inbox.get("page"))
            self.assertIn("/bootstrap#ticket=", inbox["url"])
            self.assertNotIn("next=", inbox["url"])
            self.assertNotIn(self.arachne.token, json.dumps(inbox))

    async def test_wait_heartbeats_and_explicit_cursor_replay_for_two_consumers(
        self,
    ) -> None:
        progress: list[list[tuple[float, str | None]]] = [[], []]
        progress_seen = [asyncio.Event(), asyncio.Event()]

        async def wait_once(index: int) -> dict:
            async def record(
                current: float, total: float | None, message: str | None
            ) -> None:
                del total
                progress[index].append((current, message))
                progress_seen[index].set()

            async with self.session() as session:
                result = await session.call_tool(
                    "wait_for_ruling",
                    arguments={"since": 0},
                    progress_callback=record,
                )
                return self.structured(result)

        waiters = [asyncio.create_task(wait_once(0)), asyncio.create_task(wait_once(1))]
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in progress_seen)), timeout=2
        )
        _, filed = await asyncio.to_thread(
            post_ruling, self.arachne.url, self.arachne.token, "mcp-two-consumers"
        )
        received = await asyncio.gather(*waiters)

        self.assertTrue(progress[0])
        self.assertTrue(progress[1])
        for result in received:
            self.assertEqual(result["cursor"], filed["sequence"])
            self.assertEqual(result["ruling"]["sequence"], filed["sequence"])

        # Replaying the same explicit cursor returns the same persisted ruling;
        # neither consumer owns or destructively advances server-side state.
        async with self.session() as session:
            replay = self.structured(
                await session.call_tool(
                    "wait_for_ruling", arguments={"since": 0}
                )
            )
        self.assertEqual(replay["cursor"], filed["sequence"])
