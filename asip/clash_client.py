"""Thin Clash-compatible API client.

Four tools (daemon, switch-proxy, verify_rotation, self-check) each grew their
own requests call with a different timeout, status-code policy and secret
lookup. This is the shared implementation for the operator tools and the
daemon (``daemon._CLASH``); the daemon adds a serialised connection snapshot
on top, the wire contract is the same.
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import quote

import requests

DEFAULT_TIMEOUT = 5.0
DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"


class ClashClient:
    """Minimal client for the sing-box Clash API.

    ``secret_getter`` is a callable, not a string, so a rotated secret is picked
    up on the next request instead of being frozen at import time.
    """

    def __init__(self, base_url: str, secret_getter: Callable[[], str],
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._secret_getter = secret_getter

    def _headers(self) -> dict[str, str]:
        secret = self._secret_getter() or ""
        return {"Authorization": f"Bearer {secret}"} if secret else {}

    def request(self, method: str, path: str, body: Any = None,
                timeout: float | None = None,
                params: Any = None) -> requests.Response | None:
        url = f"{self.base_url}/{path.lstrip('/')}"
        kwargs: dict[str, Any] = {
            "json": body,
            "headers": self._headers(),
            "timeout": timeout or self.timeout,
        }
        if params is not None:
            kwargs["params"] = params
        try:
            return requests.request(method, url, **kwargs)
        except requests.RequestException:
            return None

    def request_json(self, method: str, path: str, *, body: Any = None,
                     params: Any = None, timeout: float | None = None
                     ) -> tuple[int | None, Any]:
        """Return ``(status, data)``; ``(None, None)`` on transport failure.

        A non-JSON or empty response body yields ``(status, None)`` so callers
        can tell "API returned 401" from "API returned invalid JSON".
        """
        r = self.request(method, path, body=body, timeout=timeout,
                         params=params)
        if r is None:
            return None, None
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    def get_json(self, path: str, timeout: float | None = None
                 ) -> tuple[int | None, Any]:
        """Return ``(status_code, parsed_json_or_None)``."""
        return self.request_json("GET", path, timeout=timeout)

    def select(self, selector: str, value: str,
               timeout: float | None = None) -> bool:
        """Point ``selector`` at ``value``. 200 and 204 both mean success."""
        status, _ = self.request_json(
            "PUT", f"proxies/{quote(selector, safe='')}",
            body={"name": value}, timeout=timeout)
        return status in (200, 204)

    def proxy_names(self, selector: str) -> list[str] | None:
        status, data = self.get_json(f"proxies/{quote(selector, safe='')}")
        if status != 200 or not isinstance(data, dict):
            return None
        all_proxies = data.get("all")
        return [str(x) for x in all_proxies] if isinstance(all_proxies, list) else None

    def current(self, selector: str) -> str | None:
        status, data = self.get_json(f"proxies/{quote(selector, safe='')}")
        if status != 200 or not isinstance(data, dict):
            return None
        now = data.get("now")
        return str(now) if now else None

    def selector_state(self, selector: str
                       ) -> tuple[list[str] | None, str | None]:
        """Fetch ``all`` and ``now`` for ``selector`` in one GET.

        Each side is validated independently: an unusable ``all`` does not hide
        a valid ``now`` and vice versa.
        """
        status, data = self.get_json(f"proxies/{quote(selector, safe='')}")
        if status != 200 or not isinstance(data, dict):
            return None, None
        all_proxies = data.get("all")
        now = data.get("now")
        names = ([str(x) for x in all_proxies]
                 if isinstance(all_proxies, list) else None)
        current = now if isinstance(now, str) and now else None
        return names, current

    def delay(self, node: str, timeout_ms: int, url: str = DEFAULT_TEST_URL,
              *, hard_timeout: float | None = None) -> int | None:
        """Measure ``node`` latency against ``url``.

        Only a 200 response carrying a finite int/float ``delay`` counts; the
        value is normalised to ``int``. Booleans are rejected despite being
        ``int`` subclasses, and NaN/inf are rejected because ``int()`` on them
        would raise.
        """
        http_timeout = (hard_timeout if hard_timeout is not None
                        else timeout_ms / 1000 + 2)
        status, data = self.request_json(
            "GET", f"proxies/{quote(node, safe='')}/delay",
            params={"timeout": timeout_ms, "url": url},
            timeout=http_timeout)
        if status != 200 or not isinstance(data, dict):
            return None
        value = data.get("delay")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value)

    def delays(self, nodes: Any, *, workers: int = 16,
               timeout_ms: int = 15000, url: str = DEFAULT_TEST_URL,
               hard_timeout: float | None = None) -> dict[str, int | None]:
        """Measure many nodes concurrently and return ``{node: delay}``.

        The mapping is keyed by node, so callers must not depend on iteration
        order. Duplicate inputs collapse to one key.
        """
        unique = list(dict.fromkeys(nodes))
        if not unique:
            return {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                node: pool.submit(self.delay, node, timeout_ms, url,
                                  hard_timeout=hard_timeout)
                for node in unique
            }
            return {node: futures[node].result() for node in unique}

    def connections(self, timeout: float | None = None) -> dict | None:
        status, data = self.request_json("GET", "/connections",
                                         timeout=timeout)
        return data if status == 200 and isinstance(data, dict) else None
