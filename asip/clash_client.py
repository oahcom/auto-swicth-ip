"""Thin Clash-compatible API client.

Four tools (daemon, switch-proxy, verify_rotation, self-check) each grew their
own requests call with a different timeout, status-code policy and secret
lookup. This is the shared implementation for the operator tools. The daemon
keeps its own wrapper because it adds a serialised snapshot thread; the wire
contract is the same.
"""

from __future__ import annotations

from typing import Any, Callable

import requests

DEFAULT_TIMEOUT = 5.0


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
                timeout: float | None = None) -> requests.Response | None:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            return requests.request(method, url, json=body,
                                    headers=self._headers(),
                                    timeout=timeout or self.timeout)
        except requests.RequestException:
            return None

    def get_json(self, path: str, timeout: float | None = None
                 ) -> tuple[int | None, Any]:
        """Return ``(status_code, parsed_json_or_None)``.

        A dedicated pair instead of a bare dict keeps "API returned 401" and
        "API returned invalid JSON" distinguishable at the call site.
        """
        r = self.request("GET", path, timeout=timeout)
        if r is None:
            return None, None
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    def select(self, selector: str, value: str,
               timeout: float | None = None) -> bool:
        """Point ``selector`` at ``value``. 200 and 204 both mean success."""
        r = self.request("PUT", f"proxies/{selector}", {"name": value},
                         timeout=timeout)
        return r is not None and r.status_code in (200, 204)

    def proxy_names(self, selector: str) -> list[str] | None:
        status, data = self.get_json(f"proxies/{selector}")
        if status != 200 or not isinstance(data, dict):
            return None
        all_proxies = data.get("all")
        return [str(x) for x in all_proxies] if isinstance(all_proxies, list) else None

    def current(self, selector: str) -> str | None:
        status, data = self.get_json(f"proxies/{selector}")
        if status != 200 or not isinstance(data, dict):
            return None
        now = data.get("now")
        return str(now) if now else None
