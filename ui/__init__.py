"""Arachne's application-owned browser UI."""

from .render import (
    BOOTSTRAP_CSP,
    INBOX_CSP,
    fallback_title,
    font_asset,
    page_title,
    render_bootstrap,
    render_inbox,
    render_locked_inbox,
)

__all__ = [
    "BOOTSTRAP_CSP",
    "INBOX_CSP",
    "fallback_title",
    "font_asset",
    "page_title",
    "render_bootstrap",
    "render_inbox",
    "render_locked_inbox",
]
