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
from dataclasses import dataclass
from typing import Iterable

from .proc import run as proc_run

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
IPIFY_URL = "https://api.ipify.org"

# curl 退出码 → 失败原因。分得越细，「拿不到出口」才越能归因：
#   7  = 连不上 SOCKS 端口（单元没起 / 端口没监听）
#   6  = DNS 解析失败（隧道通了但节点域名解析不了）
#   56 = 收数据失败（隧道建立后被打断，典型是上游把会话掐了）
#   28 = 超时（curl 自己 --max-time 到点，进程级 timeout 轮不到）
#   22 = HTTP 错误码（隧道通了，但出口侧返回非 2xx）
# 其余归 `rc_other` 并带上原始码，别再压成笼统的 `rc`。
_CURL_RC = {
    6: "dns_fail",
    7: "connect_refused",
    22: "http_error",
    28: "timeout",
    35: "tls_fail",
    52: "empty_reply",
    56: "recv_fail",
}

# 优先级 = 「这个失败说明了多少」。高者胜出（`note` 用 >=，同级取后者）。
#
# 排序理由：
#   curl_missing —— 工具都没了，别的结论都不可信，必须冒出来；
#   timeout      —— 整段挂起，是「拿不到出口」里最需要人看的一种；
#   connect_refused / dns_fail / tls_fail / recv_fail —— 隧道层各有各的断法，
#                   都指向具体环节，比「有没有拿到地址」信息量大；
#   warp_off     —— **隧道是通的**，只是 CF 没给 WARP 会话。这条比 invalid_ip
#                   有意义得多（说明节点能用、只是 WARP 侧拒绝），所以不能沉到底；
#   http_error / rc_other —— 出口侧的问题，仍算隧道通了；
#   invalid_ip / bad_body  —— 拿到了东西但格式不对，信息量最低。
_ERROR_PRIORITY = {
    "curl_missing": 10,
    "timeout": 9,
    "connect_refused": 8,
    "dns_fail": 7,
    "tls_fail": 6,
    "recv_fail": 5,
    "empty_reply": 5,
    "warp_off": 4,
    "http_error": 3,
    "rc_other": 2,
    "invalid_ip": 1,
    "bad_body": 0,
}


@dataclass(frozen=True)
class EgressResult:
    ip: str | None
    warp: bool | None = None
    url: str | None = None
    error: str | None = None
    # Cloudflare trace 里的落点，探测时**本来就已经拿到了**，此前被直接丢掉。
    # 有了它，节点→colo 的映射不必只靠昂贵的 `warp_wg.survey()`（逐节点起
    # sing-box，一次几十秒）：任何一次生产探测都在顺手更新台账。
    colo: str | None = None
    loc: str | None = None


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
        # 经 asip.proc：超时会回收整个进程组（curl 可能拉起子进程），
        # 且错误分类统一（启动失败 vs 非零退出 vs 超时）。
        proc = proc_run(argv, timeout=timeout + 5)
        if proc.timed_out:
            note("timeout")
            continue
        if proc.error:
            note("curl_missing")
            continue
        if proc.code != 0:
            # `--max-time N` 会让 curl 自己在第 N 秒以 **退出码 28** 结束，
            # 于是 `proc.timed_out`（进程级，N+5s）永远轮不到，所有超时都被
            # 归成 `rc`。而 `rc` 同时覆盖「连接被拒」(7)、「DNS 失败」(6)、
            # 「HTTP 错误」(22)。调用方只能看到一个笼统的 rc，把三种性质
            # 完全不同的失败当作同一个「unknown」——实测 2026-11-05 远端
            # 20 条线 6009ms 超时全记成 rc，与端口没监听的 6ms rc 无区别。
            note(_CURL_RC.get(proc.code, "rc_other"))
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
            return EgressResult(ip=ip, warp=warp, url=url,
                                colo=fields.get("colo") or None,
                                loc=fields.get("loc") or None)

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
