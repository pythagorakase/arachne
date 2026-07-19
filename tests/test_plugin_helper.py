from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "plugin" / "bin" / "auth-headers.sh"
VALID_TOKEN = "a" * 40


class AuthHeadersHelperTests(unittest.TestCase):
    """Real subprocess invocations of the plugin's connect-time headersHelper."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(
        self, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        base = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        base.update(environment)
        return subprocess.run(
            ["bash", str(HELPER)],
            env=base,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def write_token(
        self, path: Path, content: str = VALID_TOKEN, mode: int = 0o600
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n", encoding="ascii")
        path.chmod(mode)
        return path

    def write_config(self, token_file: Path, mode: int) -> Path:
        config = self.home / ".config" / "arachne" / "env"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'export ARACHNE_TOKEN_FILE="{token_file}"\n', encoding="ascii"
        )
        config.chmod(mode)
        return config

    def test_emits_bearer_json_for_owner_only_token(self) -> None:
        token_file = self.write_token(self.root / "auth-token")
        result = self.run_helper({"ARACHNE_TOKEN_FILE": str(token_file)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"Authorization": f"Bearer {VALID_TOKEN}"},
        )

    def test_resolves_default_path_under_xdg_state_home(self) -> None:
        state = self.root / "state"
        self.write_token(state / "arachne" / "auth-token")
        result = self.run_helper({"XDG_STATE_HOME": str(state)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VALID_TOKEN, result.stdout)

    def test_rejects_group_readable_token(self) -> None:
        token_file = self.write_token(self.root / "auth-token", mode=0o640)
        result = self.run_helper({"ARACHNE_TOKEN_FILE": str(token_file)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-only", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_rejects_token_symlink(self) -> None:
        target = self.write_token(self.root / "real-token")
        link = self.root / "auth-token"
        link.symlink_to(target)
        result = self.run_helper({"ARACHNE_TOKEN_FILE": str(link)})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_rejects_token_outside_the_server_grammar(self) -> None:
        token_file = self.write_token(self.root / "auth-token", content='bad"token')
        result = self.run_helper({"ARACHNE_TOKEN_FILE": str(token_file)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("valid Arachne token", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_rejects_unsafe_config_instead_of_sourcing_it(self) -> None:
        token_file = self.write_token(self.root / "auth-token")
        self.write_config(token_file, mode=0o644)
        result = self.run_helper({})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner-only", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_sources_owner_only_config_for_token_path(self) -> None:
        token_file = self.write_token(self.root / "auth-token")
        self.write_config(token_file, mode=0o600)
        result = self.run_helper({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VALID_TOKEN, result.stdout)

    def test_launch_environment_beats_config(self) -> None:
        config_token = self.write_token(self.root / "config-token", content="b" * 40)
        env_token = self.write_token(self.root / "env-token")
        self.write_config(config_token, mode=0o600)
        result = self.run_helper({"ARACHNE_TOKEN_FILE": str(env_token)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(VALID_TOKEN, result.stdout)
        self.assertNotIn("b" * 40, result.stdout)
