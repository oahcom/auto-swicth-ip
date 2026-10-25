"""Egress IP lookup with one structured contract.

The project had four implementations with two endpoints and three sentinels
(``None``, ``"?"``, ``"FAIL"``). Callers could not tell "no address" from
"curl is missing". ``probe_egress`` keeps the reason on the result so a caller
can decide whether a failure is a health signal; it logs nothing and reads no
config.

``fetch_egress_ip`` remains the compatibility facade that returns only the IP.
"""

from __future__ import annotations

import ipaddress
import subprocess
from dataclasses import dataclass
from typing import Iterable

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
IPIFY_URL = "https://api.ipify.org"

_ERROR_PRIORITY = {
    "curl_missing": 6,
    "timeout": 5,
    "rc": 4,
    "invalid_ip": 3,
    "warp_off": 2,
    "bad_body": 1,
}


@dataclass(frozen=True)
class EgressResult:
    ip: str | None
    warp: bool | None = None
    url: str | None = None
    error: str | None = None


def run(*args, **kwargs):
    """Indirection so tests can patch ``subprocess.run`` or this name."""
    return subprocess.run(*args, **kwargs)


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _parse_trace(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in body.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def _proxy_args(socks_port: int | None, http_proxy: str | None,
                socks_host: str = "127.0.0.1") -> list[str]:
    if socks_port is not None:
        return ["--proxy", f"socks5h://{socks_host}:{int(socks_port)}"]
    if http_proxy is not None:
        return ["--proxy", str(http_proxy)]
    return []


def probe_egress(*, socks_port: int | None = None,
                 http_proxy: str | None = None,
                 urls: Iterable[str] = (TRACE_URL,),
                 timeout: float = 15.0,
                 require_warp: bool = False,
                 socks_host: str = "127.0.0.1") -> EgressResult:
    """Probe the public egress IP, keeping the most informative failure.

    ``socks_port`` and ``http_proxy`` are mutually exclusive. URLs are tried in
    order; the first valid address wins. With ``require_warp`` a Cloudflare
    trace reporting ``warp=off`` is skipped as ``warp_off``.
    """
    if socks_port is not None and http_proxy is not None:
        raise ValueError("socks_port and http_proxy are mutually exclusive")

    proxy_args = _proxy_args(socks_port, http_proxy, socks_host)
    last_error: str | None = None
    last_rank = -1

    def note(error: str) -> None:
        nonlocal last_error, last_rank
        rank = _ERROR_PRIORITY[error]
        if rank >= last_rank:
            last_error = error
            last_rank = rank

    for url in urls:
        argv = ["curl", "-sS", "--max-time", str(timeout), *proxy_args, url]
        try:
            proc = run(argv, capture_output=True, text=True,
                       timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            note("timeout")
            continue
        except OSError:
            note("curl_missing")
            continue

        if proc.returncode != 0:
            note("rc")
            continue

        body = proc.stdout or ""
        if "ip=" in body:
            fields = _parse_trace(body)
            warp_text = fields.get("warp")
            if require_warp and warp_text != "on":
                note("warp_off")
                continue
            raw = fields.get("ip", "")
            if not raw.strip():
                note("bad_body")
                continue
            ip = _valid_ip(raw)
            if ip is None:
                note("invalid_ip")
                continue
            warp = None
            if warp_text in ("on", "off"):
                warp = warp_text == "on"
            return EgressResult(ip=ip, warp=warp, url=url)

        raw = body.strip()
        if not raw:
            note("bad_body")
            continue
        ip = _valid_ip(raw)
        if ip is None:
            note("invalid_ip")
            continue
        return EgressResult(ip=ip, url=url)

    return EgressResult(ip=None, error=last_error or "bad_body")


def fetch_egress_ip(socks_port: int | None = None, timeout: float = 15.0,
                    urls: Iterable[str] = (TRACE_URL,),
                    require_warp: bool = False,
                    socks_host: str = "127.0.0.1") -> str | None:
    """Compatibility facade: the egress IP, or ``None`` on any failure."""
    return probe_egress(
        socks_port=socks_port,
        urls=urls,
        timeout=timeout,
        require_warp=require_warp,
        socks_host=socks_host,
    ).ip
