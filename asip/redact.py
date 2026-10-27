"""Mask credentials before they reach logs or argv.

Subscription URLs carry the account token either as ``?token=`` or inside the
path (``/api/v1/pq/<token>``). requests exception strings include the full URL,
so a single ``stderr`` log line can leak it into a world-readable file.
"""

from __future__ import annotations

import re

_TOKEN_PARAM = re.compile(r"(?i)(token=)[^&\s\"']+")
_PQ_PATH = re.compile(r"(?i)(/pq/)[0-9a-f]{16,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_USERINFO = re.compile(r"(?i)(://)[^/@\s]+@")


def redact_secrets(text: str) -> str:
    """Return ``text`` with subscription tokens and bearer values masked."""
    if not text:
        return text
    text = _TOKEN_PARAM.sub(r"\1***", text)
    text = _PQ_PATH.sub(r"\1***", text)
    text = _BEARER.sub(r"\1***", text)
    text = _URL_USERINFO.sub(r"\1***@", text)
    return text


def redact_url(url: str) -> str:
    """Keep only scheme and host so a URL can be logged verbatim."""
    if not url:
        return url
    scheme, sep, rest = url.partition("://")
    if not sep:
        return redact_secrets(url)
    host = rest.rsplit("@", 1)[-1]
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    return f"{scheme}://{host}/***"
