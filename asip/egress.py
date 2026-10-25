"""Egress IP lookup with one contract: an IP string or ``None``.

The project had four implementations with two endpoints and three sentinels
(``None``, ``"?"``, ``"FAIL"``). Callers could not tell "no address" from
"curl is missing". This helper returns ``None`` on any failure and logs nothing
— the caller decides whether a probe failure is a health signal.
"""

from __future__ import annotations

import ipaddress
import subprocess
from typing import Iterable

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
IPIFY_URL = "https://api.ipify.org"


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def fetch_egress_ip(socks_port: int | None = None, timeout: float = 15.0,
                    urls: Iterable[str] = (TRACE_URL,),
                    require_warp: bool = False) -> str | None:
    """Fetch the public egress IP through ``socks_port`` (or directly).

    ``require_warp`` drops a result whose Cloudflare trace says ``warp=off``.
    Retries the URLs in order; returns the first valid address or ``None``.
    """
    proxy_args: list[str] = []
    if socks_port:
        proxy_args = ["--proxy", f"socks5h://127.0.0.1:{int(socks_port)}"]
    for url in urls:
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", str(timeout), *proxy_args, url],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            if r.returncode != 0:
                continue
            body = r.stdout
            if "ip=" in body:
                fields = dict(
                    line.split("=", 1) for line in body.splitlines() if "=" in line
                )
                if require_warp and fields.get("warp") != "on":
                    continue
                ip = _valid_ip(fields.get("ip", ""))
            else:
                ip = _valid_ip(body)
            if ip:
                return ip
        except (OSError, subprocess.SubprocessError):
            continue
    return None
