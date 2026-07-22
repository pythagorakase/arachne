"""Build inert, LLM-readable snapshots from trusted Arachne decision pages.

Decision pages are privileged application code. Public shares are not: this
module parses the source into a small semantic tree, rejects visuals without a
text equivalent, and serializes only an explicit inert allowlist. HTML and
Markdown therefore come from one canonical representation.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlsplit


LLM_ALT_ATTRIBUTE = "data-arachne-llm-alt"
VISUAL_ATTRIBUTE = "data-arachne-visual"
DECORATIVE_ATTRIBUTE = "data-arachne-decorative"

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_DROP_TAGS = frozenset(
    {
        "base",
        "button",
        "canvas",
        "embed",
        "head",
        "iframe",
        "link",
        "meta",
        "noscript",
        "object",
        "script",
        "style",
        "svg",
    }
)
_SAFE_TAGS = frozenset(
    {
        "abbr",
        "address",
        "article",
        "aside",
        "blockquote",
        "b",
        "br",
        "caption",
        "cite",
        "code",
        "dd",
        "del",
        "details",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "i",
        "ins",
        "kbd",
        "li",
        "main",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "samp",
        "section",
        "small",
        "span",
        "strong",
        "sub",
        "summary",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "time",
        "tr",
        "u",
        "ul",
        "var",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_FORBIDDEN_ALT_TAGS = frozenset(
    {
        "audio",
        "button",
        "canvas",
        "figure",
        "form",
        "iframe",
        "img",
        "input",
        "object",
        "picture",
        "script",
        "select",
        "style",
        "svg",
        "textarea",
        "video",
    }
)
_COLLAPSE = re.compile(r"\s+")
_BLOCK_CONTAINERS = frozenset(
    {
        "article",
        "body",
        "document",
        "fieldset",
        "footer",
        "form",
        "header",
        "html",
        "main",
        "ol",
        "section",
        "table",
        "tbody",
        "template",
        "tfoot",
        "thead",
        "tr",
        "ul",
    }
)


@dataclass
class Element:
    """A deliberately small HTML tree node."""

    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[Element | str] = field(default_factory=list)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self._stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        node = Element(tag.lower(), {name.lower(): value for name, value in attrs})
        self._stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


@dataclass(frozen=True)
class Snapshot:
    """One canonical semantic snapshot and its two public representations."""

    title: str
    canonical_sha256: str
    html: str
    markdown: str


def parse_html(source: str) -> Element:
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root


def _walk(node: Element) -> Iterable[Element]:
    yield node
    for child in node.children:
        if isinstance(child, Element):
            yield from _walk(child)


def _text(node: Element) -> str:
    pieces: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            pieces.append(child)
        else:
            pieces.append(_text(child))
    return _COLLAPSE.sub(" ", "".join(pieces)).strip()


def _find_first(root: Element, tag: str) -> Element | None:
    return next((node for node in _walk(root) if node.tag == tag), None)


def _is_decorative(node: Element) -> bool:
    return (
        (node.attrs.get("aria-hidden") or "").lower() == "true"
        or DECORATIVE_ATTRIBUTE in node.attrs
    )


def _is_visual(node: Element) -> bool:
    if VISUAL_ATTRIBUTE in node.attrs or node.tag in {"figure", "img", "canvas"}:
        return True
    return node.tag == "svg" and (node.attrs.get("role") or "").lower() == "img"


def _alt_node(node: Element) -> Element | None:
    for candidate in _walk(node):
        if LLM_ALT_ATTRIBUTE in candidate.attrs:
            return candidate
    if node.tag == "img":
        ordinary_alt = _COLLAPSE.sub(" ", node.attrs.get("alt") or "").strip()
        if ordinary_alt:
            return Element("span", {LLM_ALT_ATTRIBUTE: ordinary_alt})
    if node.tag == "figure":
        for candidate in _walk(node):
            if candidate.tag != "img":
                continue
            ordinary_alt = _COLLAPSE.sub(
                " ", candidate.attrs.get("alt") or ""
            ).strip()
            if ordinary_alt:
                return Element("span", {LLM_ALT_ATTRIBUTE: ordinary_alt})
    return None


def _alt_text(node: Element) -> str:
    attribute = node.attrs.get(LLM_ALT_ATTRIBUTE)
    return _COLLAPSE.sub(" ", attribute or _text(node)).strip()


def validate_llm_alternatives(source: str) -> None:
    """Require a non-executable text equivalent for each substantive visual."""

    root = parse_html(source)

    for candidate in _walk(root):
        if LLM_ALT_ATTRIBUTE not in candidate.attrs:
            continue
        if not _alt_text(candidate):
            raise ValueError("data-arachne-llm-alt must contain a text equivalent")
        # A non-empty attribute value is the complete alternative. Descendant
        # markup remains part of the human-facing visual and is never copied.
        if (candidate.attrs.get(LLM_ALT_ATTRIBUTE) or "").strip():
            continue
        forbidden = sorted(
            {
                descendant.tag
                for descendant in _walk(candidate)
                if descendant is not candidate
                and descendant.tag in _FORBIDDEN_ALT_TAGS
            }
        )
        if forbidden:
            raise ValueError(
                "data-arachne-llm-alt must be inert semantic content; found "
                + ", ".join(forbidden)
            )

    def visit(node: Element, inside_visual: bool = False) -> None:
        if _is_decorative(node):
            return
        visual = _is_visual(node) and not _is_decorative(node)
        if visual and not inside_visual:
            alt = _alt_node(node)
            if alt is None or not _alt_text(alt):
                identity = node.attrs.get("id")
                suffix = f" #{identity}" if identity else ""
                raise ValueError(
                    f"substantive <{node.tag}>{suffix} requires "
                    "data-arachne-llm-alt (or mark it aria-hidden=true)"
                )
        for child in node.children:
            if isinstance(child, Element):
                visit(child, inside_visual or visual)

    visit(root)


def _safe_external_href(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _choice_kind(node: Element) -> str | None:
    for descendant in _walk(node):
        if descendant.tag != "input":
            continue
        kind = (descendant.attrs.get("type") or "text").lower()
        if kind in {"radio", "checkbox"}:
            return kind
    return None


def _input_description(node: Element) -> str:
    kind = (node.attrs.get("type") or "text").lower()
    if kind in {
        "button",
        "checkbox",
        "hidden",
        "image",
        "radio",
        "reset",
        "submit",
    }:
        return ""
    label = node.attrs.get("aria-label") or node.attrs.get("placeholder") or kind
    details: list[str] = []
    for attribute in ("min", "max", "step", "value"):
        value = node.attrs.get(attribute)
        if value not in {None, ""}:
            details.append(f"{attribute} {value}")
    suffix = f"; {', '.join(details)}" if details else ""
    return f"[{label} input{suffix}]"


def _sanitize_children(node: Element) -> list[Element | str]:
    result: list[Element | str] = []
    for child in node.children:
        if isinstance(child, str):
            if child.isspace():
                if node.tag not in _BLOCK_CONTAINERS:
                    result.append(" ")
            else:
                result.append(child)
        else:
            result.extend(_sanitize(child))
    return result


def _visual_alternative(node: Element) -> Element:
    alt = _alt_node(node)
    if alt is None:  # validate_llm_alternatives supplies the useful exception.
        raise ValueError("substantive visual has no LLM text equivalent")
    attribute_text = (alt.attrs.get(LLM_ALT_ATTRIBUTE) or "").strip()
    body: list[Element | str]
    if attribute_text:
        body = [Element("p", children=[attribute_text])]
        excluded = {id(alt)}
    else:
        body = _sanitize_children(alt)
        excluded = {id(candidate) for candidate in _walk(alt)}
    captions: list[Element] = []
    for candidate in _walk(node):
        if candidate.tag != "figcaption" or id(candidate) in excluded:
            continue
        caption_body = _sanitize_children(candidate)
        if caption_body:
            captions.append(
                Element(
                    "p",
                    {"class": "visual-caption"},
                    [Element("strong", children=["Caption:"]), " ", *caption_body],
                )
            )
    return Element(
        "aside",
        {"class": "visual-alt"},
        [
            Element(
                "p",
                {"class": "visual-alt-label"},
                ["Visual text equivalent"],
            ),
            *body,
            *captions,
        ],
    )


def _sanitize(node: Element) -> list[Element | str]:
    if _is_decorative(node):
        return []
    if _is_visual(node):
        return [_visual_alternative(node)]
    if node.tag in _DROP_TAGS:
        return []
    # Only a template selected by _visual_alternative() is public semantic
    # content. Other dormant templates may hold application implementation.
    if node.tag == "template":
        return []
    if node.tag == "input":
        description = _input_description(node)
        return (
            [Element("span", {"class": "control-note"}, [description])]
            if description
            else []
        )
    if node.tag == "textarea":
        if "readonly" in node.attrs:
            content = _text(node)
            return (
                [Element("pre", {"class": "control-note"}, [content])]
                if content
                else []
            )
        label = node.attrs.get("aria-label") or node.attrs.get("placeholder")
        text = f"[Free-text response{f': {label}' if label else ''}]"
        return [Element("span", {"class": "control-note"}, [text])]
    if node.tag == "select":
        options = [_text(option) for option in _walk(node) if option.tag == "option"]
        options = [option for option in options if option]
        text = "[Allowed values: " + ", ".join(options) + "]"
        return [Element("span", {"class": "control-note"}, [text])]
    if node.tag == "option":
        return []

    children = _sanitize_children(node)
    if node.tag == "label":
        choice = _choice_kind(node)
        if choice:
            marker = "Choose one: " if choice == "radio" else "May select: "
            return [Element("option", children=[marker, *children])]
        return [Element("p", children=children)]
    if node.tag == "fieldset":
        return [Element("section", {"class": "decision-group"}, children)]
    if node.tag == "legend":
        return [Element("h3", children=children)]
    if node.tag == "form":
        return [Element("section", {"class": "decision-form"}, children)]
    if node.tag == "a":
        href = _safe_external_href(node.attrs.get("href"))
        attrs = {"href": href} if href else {}
        return [Element("a", attrs, children)]
    if node.tag not in _SAFE_TAGS:
        return children

    attrs: dict[str, str | None] = {}
    if node.tag in {"td", "th"}:
        for name in ("colspan", "rowspan", "scope"):
            value = node.attrs.get(name)
            if value:
                attrs[name] = value
    if node.tag == "time" and node.attrs.get("datetime"):
        attrs["datetime"] = node.attrs["datetime"]
    if LLM_ALT_ATTRIBUTE in node.attrs:
        attrs[LLM_ALT_ATTRIBUTE] = ""
    return [Element(node.tag, attrs, children)]


def _canonical(node: Element | str) -> object:
    if isinstance(node, str):
        return node
    return {
        "tag": node.tag,
        "attrs": dict(sorted(node.attrs.items())),
        "children": [_canonical(child) for child in node.children],
    }


def _render_html_node(node: Element | str) -> str:
    if isinstance(node, str):
        return html.escape(node)
    tag = "div" if node.tag == "option" else node.tag
    attributes: list[str] = []
    for name, value in sorted(node.attrs.items()):
        if name == LLM_ALT_ATTRIBUTE:
            continue
        if value is None:
            attributes.append(name)
        else:
            attributes.append(f'{name}="{html.escape(value, quote=True)}"')
    if node.tag == "option":
        attributes.append('class="option"')
    opening = f"<{tag}{(' ' + ' '.join(attributes)) if attributes else ''}>"
    if tag in {"br", "hr"}:
        return opening
    return opening + "".join(_render_html_node(child) for child in node.children) + f"</{tag}>"


def _plain_inline(node: Element | str) -> str:
    if isinstance(node, str):
        return _COLLAPSE.sub(" ", node)
    return "".join(_plain_inline(child) for child in node.children)


def _markdown_inline(node: Element | str) -> str:
    if isinstance(node, str):
        return _COLLAPSE.sub(" ", node)
    content = "".join(_markdown_inline(child) for child in node.children)
    if node.tag in {"strong", "b"}:
        return f"**{content.strip()}**"
    if node.tag in {"em", "i"}:
        return f"*{content.strip()}*"
    if node.tag == "code":
        return f"`{content.strip().replace('`', 'ˋ')}`"
    if node.tag == "del" or node.tag == "s":
        return f"~~{content.strip()}~~"
    if node.tag == "a" and node.attrs.get("href"):
        return f"[{content.strip()}]({node.attrs['href']})"
    if node.tag == "br":
        return "  \n"
    if node.tag in {"sub", "sup"}:
        return content
    return content


def _markdown_table(node: Element) -> str:
    rows = [candidate for candidate in _walk(node) if candidate.tag == "tr"]
    matrix: list[list[str]] = []
    header_index: int | None = None
    for row in rows:
        cells = [
            child
            for child in row.children
            if isinstance(child, Element) and child.tag in {"th", "td"}
        ]
        if not cells:
            continue
        if header_index is None and any(cell.tag == "th" for cell in cells):
            header_index = len(matrix)
        matrix.append([_markdown_inline(cell).strip() for cell in cells])
    if not matrix:
        return ""
    width = max(len(row) for row in matrix)
    normalized = [row + [""] * (width - len(row)) for row in matrix]
    if header_index is None:
        normalized.insert(0, [f"Column {index + 1}" for index in range(width)])
        header_index = 0
    elif header_index != 0:
        normalized.insert(0, normalized.pop(header_index))
    safe_rows = [
        [cell.replace("|", "\\|").replace("\n", " ") for cell in row]
        for row in normalized
    ]
    lines = ["| " + " | ".join(row) + " |" for row in safe_rows]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def _render_markdown(node: Element | str, depth: int = 0) -> str:
    if isinstance(node, str):
        return _COLLAPSE.sub(" ", node)
    if node.tag == "span" and node.attrs.get("class") == "control-note":
        return _markdown_inline(node).strip() + "\n\n"
    if node.tag in {
        "a",
        "abbr",
        "cite",
        "code",
        "del",
        "em",
        "ins",
        "kbd",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
    }:
        return _markdown_inline(node)
    if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(node.tag[1])
        return f"{'#' * level} {_markdown_inline(node).strip()}\n\n"
    if node.tag == "pre":
        return f"```\n{_plain_inline(node).strip()}\n```\n\n"
    if node.tag == "hr":
        return "---\n\n"
    if node.tag == "br":
        return "  \n"
    if node.tag == "table":
        table = _markdown_table(node)
        return f"{table}\n\n" if table else ""
    if node.tag in {"thead", "tbody", "tfoot", "tr", "th", "td"}:
        return ""
    if node.tag in {"ul", "ol"}:
        ordered = node.tag == "ol"
        lines: list[str] = []
        number = 1
        for child in node.children:
            if not isinstance(child, Element) or child.tag != "li":
                continue
            content = _render_markdown(child, depth + 1).strip()
            prefix = f"{number}. " if ordered else "- "
            lines.append("  " * depth + prefix + content.replace("\n", "\n" + "  " * (depth + 1)))
            number += 1
        return "\n".join(lines) + "\n\n"
    if node.tag == "li":
        return "".join(_render_markdown(child, depth) for child in node.children)
    if node.tag == "option":
        content = re.sub(r"[ \t]+", " ", _markdown_inline(node)).strip()
        return f"- {content}\n"
    if node.tag == "aside" and node.attrs.get("class") == "visual-alt":
        content = "".join(_render_markdown(child, depth) for child in node.children).strip()
        return "\n".join(f"> {line}" if line else ">" for line in content.splitlines()) + "\n\n"
    if node.tag == "blockquote":
        content = "".join(_render_markdown(child, depth) for child in node.children).strip()
        return "\n".join(f"> {line}" for line in content.splitlines()) + "\n\n"
    rendered_children: list[str] = []
    for index, child in enumerate(node.children):
        rendered = _render_markdown(child, depth)
        if isinstance(child, Element) and child.tag == "option":
            next_child = (
                node.children[index + 1] if index + 1 < len(node.children) else None
            )
            if not isinstance(next_child, Element) or next_child.tag != "option":
                rendered += "\n"
        rendered_children.append(rendered)
    content = "".join(rendered_children)
    if node.tag in _BLOCK_TAGS or node.tag in {"option"}:
        return content.strip() + "\n\n" if content.strip() else ""
    return content


def _clean_markdown(value: str) -> str:
    lines = [line.rstrip() for line in value.splitlines()]
    result: list[str] = []
    blank = False
    for line in lines:
        if line:
            result.append(line)
            blank = False
        elif not blank and result:
            result.append("")
            blank = True
    return "\n".join(result).strip() + "\n"


_SHARE_STYLE = """
:root { color-scheme: light; --ink:#201c26; --muted:#655e6d; --paper:#fffaf0;
  --line:#d9ccba; --accent:#9d356f; --coral:#bd5b46; }
* { box-sizing:border-box; }
html { background:#eee7dc; color:var(--ink); font:17px/1.58 Georgia,serif; }
body { max-width:920px; margin:0 auto; padding:clamp(24px,5vw,64px); background:var(--paper); }
h1,h2,h3,h4,h5,h6 { line-height:1.2; margin:1.5em 0 .55em; }
h1 { margin-top:0; font-size:clamp(2rem,6vw,3.4rem); }
code,kbd,pre { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
pre { overflow:auto; padding:1rem; border:1px solid var(--line); background:#f4eddf; }
table { width:100%; border-collapse:collapse; margin:1.25rem 0; }
th,td { padding:.55rem .7rem; border:1px solid var(--line); text-align:left; vertical-align:top; }
th { background:#f1e7d8; }
.snapshot-meta { margin:0 0 2rem; padding:1rem 1.15rem; border-left:4px solid var(--accent); background:#f4eddf; color:var(--muted); }
.snapshot-meta p { margin:.2rem 0; }
.decision-form,.decision-group { margin:1.4rem 0; padding:1rem 1.15rem; border:1px solid var(--line); }
.option { margin:.65rem 0; padding:.65rem .8rem; border-left:3px solid var(--coral); background:#f8f0e4; }
.control-note { color:var(--muted); font-style:italic; }
.visual-alt { margin:1.4rem 0; padding:1rem 1.15rem; border:1px solid var(--line); background:#f1e7f0; }
.visual-alt-label { margin:0 0 .65rem; color:var(--accent); font-weight:bold; text-transform:uppercase; letter-spacing:.06em; }
a { color:var(--accent); }
@media (max-width:600px) { html { font-size:16px; } body { padding:22px 18px; } }
""".strip()


def build_snapshot(
    source: str,
    *,
    issue: str,
    created_at: str,
    expires_at: str,
) -> Snapshot:
    """Return deterministic inert HTML/Markdown from one decision-page source."""

    validate_llm_alternatives(source)
    root = parse_html(source)
    title_node = _find_first(root, "title")
    title = _text(title_node) if title_node is not None else "Arachne decision"
    title = title or "Arachne decision"
    content_root = _find_first(root, "main") or _find_first(root, "body") or root
    semantic = Element("main", children=_sanitize_children(content_root))
    canonical_bytes = json.dumps(
        _canonical(semantic), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

    meta = Element(
        "aside",
        {"class": "snapshot-meta"},
        [
            Element("p", children=["Public, read-only Arachne decision snapshot"]),
            Element("p", children=[f"Issue: {issue}"]),
            Element("p", children=[f"Created: {created_at}"]),
            Element("p", children=[f"Expires: {expires_at}"]),
            Element("p", children=[f"Content SHA-256: {canonical_sha256}"]),
        ],
    )
    body = _render_html_node(meta) + _render_html_node(semantic)
    html_document = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive,nosnippet">'
        f"<title>{html.escape(title)} — Arachne snapshot</title>"
        f"<style>{_SHARE_STYLE}</style></head><body>{body}</body></html>\n"
    )
    content_markdown = _clean_markdown(_render_markdown(semantic))
    markdown = (
        f"# {title}\n\n"
        "> Public, read-only Arachne decision snapshot  \n"
        f"> Issue: {issue}  \n"
        f"> Created: {created_at}  \n"
        f"> Expires: {expires_at}  \n"
        f"> Content SHA-256: `{canonical_sha256}`\n\n"
        f"{content_markdown}"
    )
    return Snapshot(
        title=title,
        canonical_sha256=canonical_sha256,
        html=html_document,
        markdown=markdown,
    )
