"""Durable 30-day capability shares for inert Arachne snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from semantic_snapshot import Snapshot, build_snapshot


SHARE_TTL_SECONDS = 30 * 24 * 60 * 60
SHARE_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")
SHARE_VERSION = 1


def _isoformat(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _timestamp_epoch(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("share metadata has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("share metadata timestamp must include a timezone")
    return parsed.timestamp()


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class Share:
    share_id: str
    page: str
    issue: str
    title: str
    created_at: str
    expires_at: str
    content_sha256: str
    html_sha256: str
    markdown_sha256: str
    revoked_at: str | None

    @classmethod
    def from_payload(cls, payload: object) -> "Share":
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "id",
            "page",
            "issue",
            "title",
            "created_at",
            "expires_at",
            "expires_epoch",
            "content_sha256",
            "html_sha256",
            "markdown_sha256",
            "revoked_at",
        }:
            raise ValueError("share metadata has an invalid shape")
        if payload.get("version") != SHARE_VERSION:
            raise ValueError("share metadata has an unsupported version")
        share_id = payload.get("id")
        if not isinstance(share_id, str) or SHARE_ID.fullmatch(share_id) is None:
            raise ValueError("share metadata has an invalid id")
        text_fields = {
            name: payload.get(name)
            for name in (
                "page",
                "issue",
                "title",
                "created_at",
                "expires_at",
                "content_sha256",
                "html_sha256",
                "markdown_sha256",
            )
        }
        if any(
            not isinstance(value, str) or not value
            for value in text_fields.values()
        ):
            raise ValueError("share metadata contains an invalid text field")
        for name in ("content_sha256", "html_sha256", "markdown_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", str(text_fields[name])) is None:
                raise ValueError(f"share metadata has an invalid {name}")
        expires_epoch = payload.get("expires_epoch")
        if not isinstance(expires_epoch, (int, float)) or isinstance(
            expires_epoch, bool
        ):
            raise ValueError("share metadata has an invalid expiry")
        expires_epoch = float(expires_epoch)
        if not math.isfinite(expires_epoch):
            raise ValueError("share metadata has an invalid expiry")
        created_epoch = _timestamp_epoch(str(text_fields["created_at"]))
        displayed_expiry = _timestamp_epoch(str(text_fields["expires_at"]))
        if (
            abs(displayed_expiry - created_epoch - SHARE_TTL_SECONDS) > 0.001
            or abs(displayed_expiry - expires_epoch) > 0.001
        ):
            raise ValueError("share metadata does not describe an exact 30-day expiry")
        revoked_at = payload.get("revoked_at")
        if revoked_at is not None and (
            not isinstance(revoked_at, str) or not revoked_at
        ):
            raise ValueError("share metadata has an invalid revocation time")
        if revoked_at is not None:
            _timestamp_epoch(revoked_at)
        return cls(
            share_id=share_id,
            page=str(text_fields["page"]),
            issue=str(text_fields["issue"]),
            title=str(text_fields["title"]),
            created_at=str(text_fields["created_at"]),
            expires_at=str(text_fields["expires_at"]),
            content_sha256=str(text_fields["content_sha256"]),
            html_sha256=str(text_fields["html_sha256"]),
            markdown_sha256=str(text_fields["markdown_sha256"]),
            revoked_at=revoked_at,
        )


@dataclass(frozen=True)
class ShareResult:
    share: Share
    reused: bool


class ShareStore:
    """Filesystem-backed immutable snapshots with revocable metadata."""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root.expanduser().absolute()
        self._clock = clock
        self._lock = threading.Lock()
        if self.root.is_symlink():
            raise RuntimeError(f"share store must not be a symlink: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = self.root.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
        ):
            raise RuntimeError(
                f"share store must be an owner-controlled directory: {self.root}"
            )
        os.chmod(self.root, 0o700)

    def _directory(self, share_id: str) -> Path:
        if SHARE_ID.fullmatch(share_id) is None:
            raise ValueError("invalid share id")
        return self.root / share_id

    def _metadata_path(self, share_id: str) -> Path:
        return self._directory(share_id) / "metadata.json"

    def _load_payload(self, share_id: str) -> dict[str, object]:
        directory = self._directory(share_id)
        metadata = directory / "metadata.json"
        if directory.is_symlink() or not directory.is_dir():
            raise FileNotFoundError(share_id)
        if metadata.is_symlink() or not metadata.is_file():
            raise FileNotFoundError(share_id)
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read share metadata {share_id}: {exc}") from exc
        # Full validation happens in Share.from_payload; retain expires_epoch for
        # the public availability decision.
        Share.from_payload(payload)
        return payload

    def _active(self, payload: dict[str, object]) -> bool:
        return (
            payload["revoked_at"] is None
            and float(payload["expires_epoch"]) > self._clock()
        )

    def _payload_for_snapshot(
        self,
        *,
        share_id: str,
        page: str,
        issue: str,
        created_epoch: float,
        expires_epoch: float,
        snapshot: Snapshot,
    ) -> dict[str, object]:
        html_bytes = snapshot.html.encode("utf-8")
        markdown_bytes = snapshot.markdown.encode("utf-8")
        return {
            "version": SHARE_VERSION,
            "id": share_id,
            "page": page,
            "issue": issue,
            "title": snapshot.title,
            "created_at": _isoformat(created_epoch),
            "expires_at": _isoformat(expires_epoch),
            "expires_epoch": expires_epoch,
            "content_sha256": snapshot.canonical_sha256,
            "html_sha256": hashlib.sha256(html_bytes).hexdigest(),
            "markdown_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
            "revoked_at": None,
        }

    def _matching_active(
        self,
        *,
        page: str,
        issue: str,
        title: str,
        source_sha256: str,
    ) -> Share | None:
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            if SHARE_ID.fullmatch(directory.name) is None:
                continue
            try:
                payload = self._load_payload(directory.name)
            except (FileNotFoundError, ValueError):
                continue
            if (
                payload["page"] == page
                and payload["issue"] == issue
                and payload["title"] == title
                and payload["content_sha256"] == source_sha256
                and self._active(payload)
            ):
                return Share.from_payload(payload)
        return None

    def create_or_reuse(self, *, page: str, issue: str, source: str) -> ShareResult:
        """Create one immutable 30-day snapshot, reusing an identical live one."""

        with self._lock:
            created_epoch = self._clock()
            expires_epoch = created_epoch + SHARE_TTL_SECONDS
            created_at = _isoformat(created_epoch)
            expires_at = _isoformat(expires_epoch)
            snapshot = build_snapshot(
                source,
                issue=issue,
                created_at=created_at,
                expires_at=expires_at,
            )
            existing = self._matching_active(
                page=page,
                issue=issue,
                title=snapshot.title,
                source_sha256=snapshot.canonical_sha256,
            )
            if existing is not None:
                return ShareResult(existing, reused=True)

            share_id = secrets.token_urlsafe(24)
            while (self.root / share_id).exists():
                share_id = secrets.token_urlsafe(24)
            payload = self._payload_for_snapshot(
                share_id=share_id,
                page=page,
                issue=issue,
                created_epoch=created_epoch,
                expires_epoch=expires_epoch,
                snapshot=snapshot,
            )
            temporary = Path(tempfile.mkdtemp(prefix=".share-", dir=self.root))
            try:
                _write_private(
                    temporary / "snapshot.html", snapshot.html.encode("utf-8")
                )
                _write_private(
                    temporary / "snapshot.md", snapshot.markdown.encode("utf-8")
                )
                metadata = (
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8")
                _write_private(temporary / "metadata.json", metadata)
                directory_fd = os.open(temporary, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                os.replace(temporary, self.root / share_id)
                root_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return ShareResult(Share.from_payload(payload), reused=False)

    def get(self, share_id: str) -> Share | None:
        try:
            payload = self._load_payload(share_id)
        except (FileNotFoundError, ValueError):
            return None
        if not self._active(payload):
            return None
        return Share.from_payload(payload)

    def read(self, share_id: str, format_name: str) -> tuple[Share, bytes] | None:
        """Return one verified live artifact, or None for every unavailable state."""

        if format_name not in {"html", "markdown"}:
            raise ValueError("share format must be html or markdown")
        share = self.get(share_id)
        if share is None:
            return None
        filename = "snapshot.html" if format_name == "html" else "snapshot.md"
        expected = (
            share.html_sha256
            if format_name == "html"
            else share.markdown_sha256
        )
        path = self._directory(share_id) / filename
        if path.is_symlink() or not path.is_file():
            return None
        try:
            body = path.read_bytes()
        except OSError:
            return None
        if not secrets.compare_digest(hashlib.sha256(body).hexdigest(), expected):
            return None
        # Re-check availability after reading so a concurrent revocation cannot
        # leave a newly opened response valid indefinitely.
        if self.get(share_id) is None:
            return None
        return share, body

    def revoke(self, share_id: str) -> bool:
        """Make one live share unavailable while retaining an owner-only tombstone."""

        with self._lock:
            try:
                payload = self._load_payload(share_id)
            except (FileNotFoundError, ValueError):
                return False
            if not self._active(payload):
                return False
            payload["revoked_at"] = _isoformat(self._clock())
            destination = self._metadata_path(share_id)
            temporary = destination.with_name(
                f".metadata.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(
                        payload,
                        stream,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
            return True
