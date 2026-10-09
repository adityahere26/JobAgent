"""Turning untrusted job text into safe, plain, contact-free text.

Everything here is pure and offline. Job descriptions are untrusted input: they are reduced to
plain text, stripped of recruiter contact details, and length-capped before anything else sees them.
"""

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

CONTACT_PLACEHOLDER = "[contact removed]"

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "section", "article", "table", "header", "footer",
}  # fmt: skip
_SKIP_TAGS = {"script", "style"}


class _TextExtractor(HTMLParser):
    """HTML to plain text. Script/style content and mailto:/tel: link text are dropped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._in_contact_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "a":
            href = (dict(attrs).get("href") or "").strip().lower()
            if href.startswith(("mailto:", "tel:")):
                self._in_contact_link = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "a":
            self._in_contact_link = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and not self._in_contact_link:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_EMAIL_DISGUISED = re.compile(
    r"[\w.+\-]+\s*[\[(]\s*at\s*[\])]\s*[\w\-]+(?:\s*[\[(]\s*dot\s*[\])]\s*[\w\-]+)+",
    re.IGNORECASE,
)
# International: starts with "+", 8-15 digits in total.
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d[\d\s\-().]{6,18}\d")
# Indian mobile written with a space or dash in the middle, e.g. 98765 43210 or 098765-43210.
_PHONE_IN_GROUPED = re.compile(r"(?<![\d+])(?:0[\s\-]?)?[6-9]\d{4}[\s\-]\d{5}(?!\d)")
# Any number right after a word like "call" or "WhatsApp", so bare 10-digit numbers are caught
# only when the context says it is a phone number (salaries, years and job codes are left alone).
_PHONE_KEYWORD = re.compile(
    r"(?i)\b(call|phone|mobile|mob|whatsapp|contact|tel|reach)\b([^\n\d+]{0,20})"
    r"(\+?\d[\d\s\-().]{7,16}\d)"
)


def _digit_count(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def strip_contacts(text: str) -> str:
    """Replace recruiter emails and phone numbers with a placeholder. Best-effort."""
    text = _EMAIL.sub(CONTACT_PLACEHOLDER, text)
    text = _EMAIL_DISGUISED.sub(CONTACT_PLACEHOLDER, text)

    def intl(match: re.Match[str]) -> str:
        return CONTACT_PLACEHOLDER if 8 <= _digit_count(match.group()) <= 15 else match.group()

    def keyword(match: re.Match[str]) -> str:
        if 8 <= _digit_count(match.group(3)) <= 15:
            return f"{match.group(1)}{match.group(2)}{CONTACT_PLACEHOLDER}"
        return match.group()

    text = _PHONE_INTL.sub(intl, text)
    text = _PHONE_IN_GROUPED.sub(CONTACT_PLACEHOLDER, text)
    return _PHONE_KEYWORD.sub(keyword, text)


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿]")


def truncate(text: str, limit: int) -> str:
    """Cap at `limit` characters, preferring a word boundary, adding an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    boundary = cut.rfind(" ")
    if boundary > limit * 0.8:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


def normalize_text(text: str, limit: int) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", _CONTROL.sub("", text))
    text = re.sub(r"[ \t\f\v ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return truncate(text, limit)


def clean_line(text: str, limit: int) -> str:
    """Single-line field (title, company, location): no control chars, single spaces."""
    text = _INVISIBLE.sub("", _CONTROL.sub("", text))
    return truncate(re.sub(r"\s+", " ", text).strip(), limit)


def clean_description(raw: str, limit: int = 20_000) -> str:
    """Full pipeline for a job description. Input may be HTML or plain text."""
    return normalize_text(strip_contacts(html_to_text(raw)), limit)


def validate_https_url(url: str) -> str:
    url = url.strip()
    if len(url) > 2000 or re.search(r"[\s\x00-\x1f]", url):
        raise ValueError("URL is empty, too long, or contains whitespace")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("URL must be an https URL with a host")
    if parts.username or parts.password:
        raise ValueError("URL must not contain credentials")
    return url
