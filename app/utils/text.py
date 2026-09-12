"""Telegram text utilities: escaping, markdown→HTML conversion and chunking.

LLMs leak markdown even when told not to.  ``normalise()`` converts the common
patterns (``**bold**``, ``# heading``, ``- bullet``, tables, code fences) into
Telegram-safe HTML so the user always sees a clean message.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable

from app.constants import TELEGRAM_CHUNK_LIMIT, TELEGRAM_MESSAGE_LIMIT

_CODE_FENCE = re.compile(r"```[\w]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s*(.+?)\s*#*\s*$", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_US = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<!\*)\*([^\*\n]+)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BULLET = re.compile(r"^(\s*)[\-\*]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^(\s*)(\d+)\.\s+", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_URL_BARE = re.compile(r"(?<![\w\">/])(https?://[^\s<>\"']+)")


def escape_html(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def normalise(text: str) -> str:
    """Convert LLM markdown into Telegram HTML, escaping raw angle brackets."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # protect code blocks (show as monospace)
    code_blocks: list[str] = []

    def _grab_code(match: re.Match[str]) -> str:
        code_blocks.append(match.group(1).strip())
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    text = _CODE_FENCE.sub(_grab_code, text)

    # markdown tables → readable bullet lines
    def _table_line(match: re.Match[str]) -> str:
        row = match.group(0).strip().strip("|")
        if set(row.replace("|", "").replace(" ", "")) <= set("-:"):
            return ""  # separator row
        cells = [c.strip() for c in row.split("|")]
        return "• " + " — ".join(c for c in cells if c)

    text = _TABLE_ROW.sub(_table_line, text)

    # links [label](url) → HTML anchor
    text = _MD_LINK.sub(lambda m: f'<a href="{escape_html(m.group(2))}">{escape_html(m.group(1))}</a>', text)

    # headings → bold lines
    text = _HEADING.sub(lambda m: f"\n<b>{escape_html(m.group(2))}</b>", text)

    # bullets
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _NUMBERED.sub(lambda m: f"{m.group(1)}{m.group(2)}. ", text)

    # escape everything else, then re-apply emphasis
    text = escape_html(text)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_US.sub(r"<b>\1</b>", text)
    text = _ITALIC.sub(r"<i>\1</i>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _INLINE_CODE.sub(r"<code>\1</code>", text)

    def _restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return f"<pre>{escape_html(code_blocks[index])}</pre>"

    text = re.sub(r"\x00CODE(\d+)\x00", _restore, text)

    # bare URLs → clickable
    text = _URL_BARE.sub(lambda m: m.group(0) if "/a>" in m.group(0) else f'<a href="{m.group(0)}">🔗 رابط</a>', text)

    # tidy whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def plain(text: str) -> str:
    """Strip every tag — used for PDF/charts where HTML is meaningless."""
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


def chunk(text: str, limit: int = TELEGRAM_CHUNK_LIMIT) -> list[str]:
    """Split a long message on safe boundaries (paragraph → line → word)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if len(block) <= limit:
            parts.append(block)
            continue
        current = ""
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                parts.append(current)
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        if current:
            parts.append(current)

    # merge adjacent chunks that still fit
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) + len(part) + 2 <= limit:
            merged[-1] = f"{merged[-1]}\n\n{part}"
        else:
            merged.append(part)
    return merged


def bullet_list(items: Iterable[str], *, bullet: str = "•") -> str:
    return "\n".join(f"{bullet} {item}" for item in items if item)


def truncate(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT - 1) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mask_id(tg_id: int) -> str:
    return f"<code>{tg_id}</code>"


def money(value: float, currency: str = "$") -> str:
    return f"{currency}{value:,.2f}"


def compact_number(value: float | int) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}".rstrip("0").rstrip(".")
    return f"{value:,}"
