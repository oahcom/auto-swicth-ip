"""Shared HTTP helper for the auto-switch-ip entry points.

Four tools used to call ``requests`` with their own retry policy, their own
JSON handling and their own error shape. The differences were accidental: a
failed connection and "the server answered 500" collapsed into the same
``None``, so the caller could not tell "never reached the host" from "host
rejected the request".

``status is None`` is reserved for a transport failure (DNS, connect, read
timeout), while a non-success status means a server did answer and supplied a
body. That distinction matters here because the ledgers decide whether a node
is burned based on whether a probe actually ran; retrying or reclassifying a
refused connection would misattribute the failure to the node.

Retries default to 0 on purpose. A GET is safe to repeat, but the same helper
serves ``PUT /proxies`` — retrying a switch after a timeout can move a line
twice. Callers that know their request is idempotent opt in explicitly.
``backoff`` grows linearly with the attempt number so a short outage is not
hammered.

This module does no logging, reads no environment variables and loads no
config: every endpoint, header and secret is passed in by the caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from .redact import redact_secrets

_TEXT_LIMIT = 2000


@dataclass(frozen=True)
class HttpResult:
    """Outcome of one logical request, including any retries it made."""

    status: int | None
    data: Any = None
    text: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        # 与 requests.raise_for_status 一致：全部 2xx 都算成功。旧调用点用
        # 200/204 白名单时，201/206 会被误判成失败（订阅源返回 206 分片即中招）。
        return self.status is not None and 200 <= self.status < 300


def request_json(
    method: str,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: Any = None,
    retries: int = 0,
    backoff: float = 0.2,
    redact: bool = True,
    session: requests.Session | None = None,
    text_limit: int | None = _TEXT_LIMIT,
) -> HttpResult:
    """Send a request and decode its body when the body is JSON.

    ``timeout`` is mandatory so no call site can silently hang forever, and it
    must be positive. ``body`` is sent as JSON. ``session`` lets a caller reuse
    a connection pool or inject a test double; without it the module-level
    ``requests.request`` is used.

    Transport failures are retried up to ``retries`` additional times, waiting
    ``backoff * attempt`` between tries. The final failure returns
    ``status=None`` with the exception text in ``error`` (masked by default,
    because ``requests`` exceptions embed the full URL including its token).

    A JSON body becomes ``data``. Anything else, including an HTML error page,
    lands in ``text`` truncated to 2000 characters so a stray gateway page
    cannot flood a log. Callers that need the full payload (a subscription is a
    base64 blob many kilobytes long) pass ``text_limit=None``; truncating it
    would silently drop nodes. 204 and empty bodies are not parsed at all.
    """
    if timeout is None or not timeout > 0:
        raise ValueError("timeout must be a positive number")

    request = session.request if session is not None else requests.request
    kwargs: dict[str, Any] = {"timeout": timeout}
    if headers is not None:
        kwargs["headers"] = headers
    if params is not None:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body

    last_error: BaseException | None = None
    response: requests.Response | None = None
    for attempt in range(retries + 1):
        try:
            response = request(method, url, **kwargs)
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                break
            if backoff:
                time.sleep(backoff * (attempt + 1))

    if response is None:
        message = str(last_error) if last_error is not None else "request failed"
        return HttpResult(
            status=None,
            error=redact_secrets(message) if redact else message,
        )

    status = response.status_code
    if status == 204:
        return HttpResult(status=status)

    raw = response.text or ""
    if not raw.strip():
        return HttpResult(status=status)

    try:
        return HttpResult(status=status, data=response.json())
    except ValueError:
        if text_limit is None:
            return HttpResult(status=status, text=raw)
        return HttpResult(status=status, text=raw[:text_limit])
