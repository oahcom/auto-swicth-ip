#!/usr/bin/env python3
"""WARP-over-proxy 线路：一条线一个 sing-box 进程，WG 的 UDP 经代理节点中转。

和现有 usque 线（MASQUE over TCP/443，直连）并列使用，共用 daemon 的
restart / probe / selector 语义。

WHY 每条线一个进程，而不是把 endpoint 塞进主 sing-box：
  WARP 出口 IP 只随「会话重连」和「落地 colo」变化。主 sing-box 是单进程，
  重启它会掐断全部代理流量；per-line 进程让「只重连这一条线」成立，
  daemon 的 `_restart_service` / `_probe_refresh_egress` 原样可用。

WHY detour 绑定具体节点，而不是主池的 7890：
  实测出口 IP 由 WARP 落地 colo 决定，同一 colo 下不同设备拿到同一地址
  （device1/device2 均为 104.28.222.43），换节点则从 SG 变 JP。要让 N 条线
  拿到 N 个地址，必须让每条线落到不同节点；共用 7890 会把所有线压进同一个 colo。

本地到 WARP 的 UDP 2408 被电信 ACL 拦（27.50.59.99 回 ICMP code 13），
所以 endpoint 必须带 detour，由代理节点代为发出 UDP。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from asip.atomic_io import write_json
from config import (AIRPORT_DNS_HOST, AIRPORT_DNS_PATH, AIRPORT_DNS_SUFFIXES,
                    AIRPORT_DNS_TAG, CFG, PROJECT_DIR, needs_airport_dns)

# 渠道模型（块 = colo）。import 方向只能是 warp_wg → warp_blocks：模型不知道
# 线路、端口、单元名这些东西，反过来 import 会让「纯模型」变成第二个 daemon。
import warp_blocks as blocks

# config.py 导出的 PROJECT_DIR 是 str（历史原因）；路径运算统一走 Path。
PROJECT = Path(PROJECT_DIR)

WGCF_TAG_RE = re.compile(r"^(?P<key>[A-Za-z]+)\s*=\s*(?P<val>.+)$")


# ---------------------------------------------------------------- 路径 / 命名

def _wg() -> dict:
    return CFG["warp"]["wg"]


def line_tag(i: int) -> str:
    return f"{_wg()['tag_prefix']}{i}"


def unit_name(i: int) -> str:
    return f"{_wg()['service_prefix']}{i}.service"


def line_dir(i: int) -> Path:
    return PROJECT / ".warp" / "wglines" / f"line{i}"


def state_path() -> Path:
    return PROJECT / ".warp" / "warp_wg_state.json"





# ---------------------------------------------------------------- WARP 注册

def register_line(i: int, force: bool = False) -> None:
    """注册一条线的 WARP 设备并生成 WireGuard profile。

    每个账号是独立的 Cloudflare 设备，凭据落 `line{i}/`（mode 600，gitignored）。
    已存在则跳过：重复注册会换掉正在使用的密钥，让在跑的隧道直接掉线。
    """
    d = line_dir(i)
    d.mkdir(parents=True, exist_ok=True)
    acc, prof = d / "account.toml", d / "profile.conf"
    if prof.exists() and not force:
        return
    wgcf = _wg()["wgcf_bin"]
    if not os.access(wgcf, os.X_OK):
        raise SystemExit(f"缺少 wgcf 可执行文件: {wgcf}（见 docs/warp-wg-over-proxy.md）")
    _register_with_backoff(wgcf, acc, line_tag(i))
    _run([wgcf, "generate", "--config", str(acc), "-p", str(prof)])
    for p in (acc, prof):
        os.chmod(p, 0o600)


def _register_with_backoff(wgcf: str, acc: Path, tag: str,
                           delays: tuple[int, ...] = (30, 90, 240)) -> None:
    """注册设备，遇到 Cloudflare 429 时退避重试。

    实测一个窗口内约 9 台就被限流；没有退避的话第 10 条线会让整批 apply
    中断，前面已经注册好的设备全白等。
    """
    for wait in delays:
        try:
            _run([wgcf, "register", "--config", str(acc),
                  "--accept-tos", "-n", tag])
            return
        except RuntimeError as e:
            if "429" not in str(e):
                raise
            print(f"  ⏳ {tag} 注册被限流，{wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
    _run([wgcf, "register", "--config", str(acc), "--accept-tos", "-n", tag])


def _run(argv: list[str]) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"{argv[0]} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def read_profile(path: Path) -> dict:
    """解析 wgcf 生成的 profile.conf（INI 风格，非 JSON）。

    Address 是逗号分隔的地址列表；Endpoint 可能是 `host:port` 或 `[v6]:port`，
    但当前实现只用 Peer.PublicKey，peer 地址由配置里的 peer_v4/peer_v6 决定。
    """
    iface: dict[str, str] = {}
    peer: dict[str, str] = {}
    section = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[]").lower()
            continue
        m = WGCF_TAG_RE.match(line)
        if not m:
            continue
        (peer if section == "peer" else iface)[m["key"]] = m["val"].strip()
    if not iface.get("PrivateKey") or not peer.get("PublicKey") or not iface.get("Address"):
        raise ValueError(f"profile 不完整: {path}")
    return {
        "private_key": iface["PrivateKey"],
        "address": [a.strip() for a in iface["Address"].split(",") if a.strip()],
        "peer_public_key": peer["PublicKey"],
    }


# ---------------------------------------------------------------- 节点选择

def load_nodes() -> list[dict]:
    """从已生成的 singbox-config.json 取可用出口节点。

    不重新拉订阅：那份文件是 refresh 的产物，此刻的节点集合与主池一致，
    避免两处各自解析出一份 tag 对不上的节点。
    """
    try:
        cfg = json.loads(Path(CFG["singbox"]["config_file"]).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"读取 {CFG['singbox']['config_file']} 失败: {e}")
    nodes = [o for o in cfg.get("outbounds", [])
             if o.get("type") not in ("selector", "direct", "block", "socks")
             and o.get("server") and o.get("server_port")]
    return nodes


def node_group(node: dict) -> str:
    """节点多样性分组键：取主机名前两段（pq.aws48 / unamecf.xn--…）。

    只看首段会把 pq.aws48..64 全部并成一组，等于没有多样性。
    """
    host = str(node.get("server") or "")
    return ".".join(host.split(".")[:2]) or host


def colo_map_path() -> Path:
    return PROJECT / ".warp" / "wg_colo_map.json"


def load_colo_map() -> dict:
    try:
        data = json.loads(colo_map_path().read_text())
    except (OSError, ValueError):
        return {}
    return data.get("nodes") or {}


def region_key(node: dict) -> str:
    """出口分组键：优先用实测的 (colo, v4)，没有实测才退回主机名。

    WHY 不能用主机名：aws48/aws49 是两台不同主机，但 WARP 落地都是 NRT，
    v4 出口同为 104.28.211.105 —— 按主机名分组等于把 10 个 NRT 节点拆成
    10 组，分配出来的 10 条 v4 线只有 2 个真实地址。
    """
    e = load_colo_map().get(node.get("tag")) or {}
    # 用 colo 而不是 colo+具体地址：v4 地址在两次 survey 之间会漂（同节点
    # 从 .243 变 .211），拿它当分组键会让每次 apply 得到不同拓扑。colo 稳定。
    if e.get("colo"):
        return str(e["colo"])
    return node_group(node)


def assign_nodes(nodes: list[dict], count: int, start: int = 0,
                 key=None, rank=None) -> list[dict]:
    """按分组轮转挑节点，尽量让每条线落在不同主机。

    这不是 colo 保证（落地位置由节点出口决定，只能实测），但能避免 N 条线
    全部指向同一台机器的不同端口。

    ``rank`` 给分组键排先后。WHY 需要它：分组键里混着实测 colo（NRT/SIN/
    PHX/SJC/LAX）和未实测时的主机名兜底（203.10 / downloadcfpro…）。按
    字符串排序会把 "203.10" 排到 "NRT" 前面，于是一个没实测 colo 的节点
    抢走一个落地名额，真有 colo 的反被挤掉 —— 那就不是按 colo 分配了。
    """
    key = key or node_group
    groups: dict[str, list[dict]] = {}
    for n in nodes:
        groups.setdefault(key(n), []).append(n)
    order = sorted(groups, key=(lambda k: (rank(k), k)) if rank else None)
    buckets = [groups[k] for k in order]
    picked: list[dict] = []
    idx = start
    while len(picked) < count and any(buckets):
        b = buckets[idx % len(buckets)]
        if b:
            picked.append(b.pop(0))
        idx += 1
        if not any(buckets):
            break
    if len(picked) < count:
        raise SystemExit(f"可用节点不足：需要 {count} 条线，只有 {len(picked)} 个节点")
    return picked


# ---------------------------------------------------------------- 配置生成

def _split_peer(peer: str) -> tuple[str, int]:
    """`1.2.3.4:2408` / `[v6]:2408` → (host, port)。"""
    if peer.startswith("["):
        host, _, rest = peer.partition("]")
        return host.lstrip("["), int(rest.lstrip(":"))
    host, _, port = peer.rpartition(":")
    return host, int(port)


def prepare_node(node: dict) -> dict:
    """单线配置里的节点出站：入口是机场域名时，必须挂机场 DoH。

    WHY 不是「本机 DNS 也能解析」：机场拿 `cn.bing.com` / `us.bing.com` /
    `hk-aws-*.acczzz.com` 这些**别人的域名**当跳板，公网上它们要么 NXDOMAIN，
    要么解析到真 Bing（202.89.233.x）。真 Bing 的证书是 Microsoft 的，
    机场前置要的是 `*.okzdns.com` —— 于是握手报「证书不属于本域」，
    看起来像节点坏了。2026-11-01 实测：补上 `domain_resolver` 后 okz 系
    节点 0/54 → 50/54（TEST_PLAN v1.57）。

    主配置由 `parse_subscription` 负责这件事；单线配置原先漏了，后果是
    「块模型挑出来的节点一写成线就连不上」—— 而这批节点恰恰是实测 WARP
    可用的那批。
    """
    n = json.loads(json.dumps(node))
    if needs_airport_dns(n.get("server")):
        n["domain_resolver"] = AIRPORT_DNS_TAG
    else:
        n.setdefault("domain_resolver", "dns-default")
    return n


def build_line_config(profile: dict, port: int, node: dict, peer: str,
                      family: str = "v6") -> dict:
    """单条线的完整 sing-box 配置。

    ``family`` 决定 DNS 解析偏好，也就决定出口地址族：WARP 的 v6 地址每次
    重连都换（实测 4/4 换新，连 /64 都变），v4 地址由落地 colo 决定（同 colo
    重复）。所以 v6 渠道靠重启换地址，v4 渠道只能靠换节点。

    两个偏好都保留双栈回退：``prefer_ipv4`` 遇到只有 AAAA 的目标仍走 v6，
    反之亦然。不能用 ``ipv4_only``——那会让只有 AAAA 的目标直接不可达。
    """
    host, peer_port = _split_peer(peer)
    node = prepare_node(node)
    node_tag = node["tag"]
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{
            "type": "socks", "tag": "in",
            "listen": _wg()["listen_host"], "listen_port": port,
        }],
        "outbounds": [node],
        # 两个 DNS server，和主配置同一形态：
        #   dns-airport —— 只解析机场域名（节点入口），必须走它才拿到前置 IP；
        #   dns-default —— 本机解析器，兜住其余一切，也是 dns-airport 自己的
        #                  resolver（指回自己会 circular，实测 FATAL）。
        # WHY 不做 detour：节点 server 是域名，若 DNS 也 detour 到该节点，
        # 解析节点本身就要先连上节点 —— 环路，sing-box 报
        # "DNS query loopback in transport[dns]"，握手永远起不来。
        "dns": {
            "servers": [
                {"type": "https", "tag": AIRPORT_DNS_TAG,
                 "server": AIRPORT_DNS_HOST, "path": AIRPORT_DNS_PATH,
                 "domain_resolver": "dns-default"},
                {"type": "local", "tag": "dns-default"},
            ],
            # 机场域名钉死 ipv4：前置只有 A 记录，走 v6 会拿到空答案后回落，
            # 白等一轮超时。这条规则只影响这些域名，不影响隧道出口的选择。
            "rules": [{"domain_suffix": list(AIRPORT_DNS_SUFFIXES),
                       "server": AIRPORT_DNS_TAG, "strategy": "ipv4_only"}],
            "final": "dns-default",
            "strategy": "prefer_ipv4" if family == "v4" else "prefer_ipv6",
        },
        "endpoints": [{
            "type": "wireguard", "tag": "warp",
            "address": profile["address"],
            "private_key": profile["private_key"],
            "mtu": _wg()["mtu"],
            "peers": [{
                "address": host, "port": peer_port,
                "public_key": profile["peer_public_key"],
                "allowed_ips": ["0.0.0.0/0", "::/0"],
                "reserved": [0, 0, 0],
                "persistent_keepalive_interval": _wg()["keepalive"],
            }],
            # 本地 UDP 2408 被运营商 ACL 拦，必须经节点中转。
            "detour": node_tag,
        }],
        "route": {"final": "warp",
                  "default_domain_resolver": {"server": "dns-default"}},
    }


def line_family(i: int) -> str:
    fams = _wg().get("families") or ["v6"]
    return fams[(i - 1) % len(fams)]


def write_line(i: int, node: dict, peer: str | None = None,
               family: str | None = None) -> dict:
    d = line_dir(i)
    d.mkdir(parents=True, exist_ok=True)
    profile = read_profile(d / "profile.conf")
    cfg = build_line_config(profile, _wg()["base_port"] + i - 1, node,
                            peer or _wg()["peer_v4"],
                            family or line_family(i))
    path = d / "singbox.json"
    write_json(str(path), cfg, mode=0o600, indent=2)
    # 以 root 跑（sudo / 安装 / 排障）时，0600 + root 属主会让以 User=administrator
    # 启动的单元报 `FATAL read config: permission denied` —— 而且报的是「读不了配置」，
    # 极易误判成文件损坏或 sing-box 版本问题（实测踩过一次）。属主一律对齐目录属主：
    # 目录是安装时以运行用户建的，所以它就是该跑这条线的身份。
    try:
        if os.getuid() == 0:
            owner = d.stat()
            cur = path.stat()
            if (cur.st_uid, cur.st_gid) != (owner.st_uid, owner.st_gid):
                os.chown(path, owner.st_uid, owner.st_gid)
    except Exception:
        pass
    return cfg


def refresh_main_outbounds() -> None:
    """重写 .warp/warp-outbound.json —— 主 sing-box 出站的唯一数据源。

    config.py 在 import 时已按 warp.wg 追加好成员，这里只是把当前 CFG 落盘。
    不另建一份文件，否则usque 与 WG 出口会各自漂移。
    """
    _run([sys.executable, str(PROJECT / "deploy" / "members.py"),
          "--write-warp-outbound"])


# ---------------------------------------------------------------- 探测

def _socks_udp(proxy_port: int, dst: tuple[str, int], payload: bytes,
               timeout: float = 6.0) -> bytes | None:
    """经本地 SOCKS5 做一次 UDP 往返（RFC 1928 UDP ASSOCIATE）。

    超时/对端拒绝都返回 None：探测的语义是「能不能用」，不是「为什么不能用」，
    异常逃出去会让整批 --probe 在第一台坏节点上就中断。
    """
    try:
        return _socks_udp_inner(proxy_port, dst, payload, timeout)
    except OSError:
        return None


def _socks_udp_inner(proxy_port: int, dst: tuple[str, int], payload: bytes,
                     timeout: float) -> bytes | None:
    t = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    try:
        t.sendall(b"\x05\x01\x00")
        if t.recv(2) != b"\x05\x00":
            return None
        t.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        r = t.recv(10)
        if len(r) < 10 or r[1] != 0:
            return None
        relay = (socket.inet_ntoa(r[4:8]) if r[3] == 1 else "0.0.0.0",
                 struct.unpack("!H", r[8:10])[0])
        if relay[0] in ("0.0.0.0", "::"):
            relay = ("127.0.0.1", relay[1])
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(timeout)
        try:
            u.sendto(b"\x00\x00\x00\x01" + socket.inet_aton(dst[0])
                     + struct.pack("!H", dst[1]) + payload, relay)
            data, _ = u.recvfrom(2048)
            return data
        finally:
            u.close()
    finally:
        t.close()


def node_outbound(tag: str) -> dict | None:
    """按 tag 从生产配置取节点 outbound（已含 correct server_name/凭据）。

    不要手搓节点配置：SNI 与凭据必须是订阅给的那份，手写会得到
    `bilibili-image-cdn.hi2026.net` 这类过期的 server_name（实测踩过）。
    """
    try:
        cfg = json.loads(Path(CFG["singbox"]["config_file"]).read_text())
    except (OSError, ValueError):
        return None
    for o in cfg.get("outbounds", []):
        if o.get("tag") == tag and o.get("server"):
            return o
    return None


def probe_node_tcp(node: dict, timeout: float = 6.0,
                   prod_dns: dict | None = None) -> bool | None:
    """经临时 sing-box 验证节点能否转发 TCP 到 Cloudflare。None = 测不了。

    WHY 不能在本进程里直接 `socket.create_connection(节点域名)`：机场节点域名
    只由订阅里的 DoH 解析得了，系统 DNS 一律超时。实测 19 条线全部
    `TimeoutError/gaierror`，其中 8 条当时正在正常出流量 —— 拿系统 DNS 的
    失败当节点死亡，会把整批健康线判死并无谓重启。

    这里让 sing-box 自己解析（它配了无人机场的 DNS）并只连一个已知稳定的
    目标（1.1.1.1:443），所以结论只反映「节点→公网」这一段。
    """
    sb = CFG["singbox"]["binary"]
    if prod_dns is None:
        try:
            prod_dns = json.loads(
                Path(CFG["singbox"]["config_file"]).read_text())["dns"]
        except (OSError, ValueError, KeyError):
            return None
    with tempfile.TemporaryDirectory(prefix="tcpprobe-") as td:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cfg = {
            "log": {"level": "error"},
            "inbounds": [{"type": "socks", "listen": "127.0.0.1",
                          "listen_port": port}],
            "outbounds": [node],
            # 必须复用生产 DNS：节点域名（juhazf.cn / acczzz.com 等）只有机场
            # DoH 解析得了，系统 DNS 一律超时。用 dns-default 会把每条线都
            # 探成 None（实测 19/19），探测等于没做。
            "dns": prod_dns,
            "route": {"final": node["tag"],
                      "default_domain_resolver": {"server": "dns-airport"}},
        }
        path = os.path.join(td, "probe.json")
        write_json(path, cfg, mode=0o600, indent=2)
        proc = subprocess.Popen([sb, "run", "-c", path],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            for _ in range(25):
                try:
                    socket.create_connection(("127.0.0.1", port),
                                             timeout=0.3).close()
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                return None
            try:
                sock = socket.create_connection(("127.0.0.1", port),
                                                timeout=timeout)
            except OSError:
                return None
            try:
                sock.sendall(b"\x05\x01\x00")
                if sock.recv(2) != b"\x05\x00":
                    return None
                sock.sendall(b"\x05\x01\x00\x01"
                             + socket.inet_aton("1.1.1.1")
                             + struct.pack("!H", 443))
                rep = sock.recv(10)
                return len(rep) >= 2 and rep[1] == 0
            except OSError:
                # 握手/中继阶段超时 = 节点接了本地端口却不转发，正是要测的那件事。
                # 让它抛出去会把整轮同块轮回打成异常（实测 3h 18 次
                # `✗ 同块轮回异常: timed out`），一条坏节点拖住所有线换节点。
                return False
            finally:
                sock.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def udp_capable(nodes: list[dict], probe: bool = False) -> list[dict]:
    """**已废弃的 STUN 判据，现在只做原样透传。**

    历史：这里曾用临时 sing-box 向 162.159.207.0:3478 发 STUN，结论落
    `.warp/wg_node_udp.json` 缓存 6h。它是错的 —— WARP 走 2408，节点完全可以
    只放行 2408 而不放行 3478。实测 `pq.aws48` STUN=False，但它承载的
    WARP-WG-5 正在正常出 v6 流量（colo=NRT）。缓存里 228 条有 192 条记成
    False，`apply()` 据此把 22 条渠道砍到 8 条，v4 合并块直接清空。

    节点能否承载 WARP 只能靠**实际握手**验证：`apply()` 之后每条线跑一次
    出口探测（`_trace_fields`），daemon 的同块轮回守护也在持续做这件事。
    `probe` 参数保留只为不破坏调用方签名，传什么都不影响结果。
    """
    return list(nodes)


TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"


def _trace_fields(port: int, timeout: float = 6.0) -> dict[str, str]:
    """经线路 socks 端口读一次 Cloudflare trace。失败返回空 dict。"""
    try:
        r = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout),
             "--proxy", f"socks5h://127.0.0.1:{port}", TRACE_URL],
            capture_output=True, text=True, timeout=timeout + 5)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


def survey(timeout: float = 20.0, only: list[str] | None = None,
           merge: bool = True) -> dict:
    """实测每个节点经 WARP 落地后的 colo / loc / v4，落 wg_colo_map.json。

    分配拓扑前跑一次。只有 colo 是稳定维度：v4 是每个会话从该 colo 的小池子
    里随机抽的，与节点无关（同一节点连续重启会在 .211/.243 之间跳）。所以
    这张表里的 v4 只作参考，不能当节点的固定属性用。
    """
    pool = udp_capable(load_nodes(), probe=True)
    if only:
        want = set(only)
        pool = [n for n in pool if n["tag"] in want]
    prof_path = line_dir(1) / "profile.conf"
    if not prof_path.exists():
        register_line(1)
    profile = read_profile(prof_path)
    port = _wg()["base_port"] + 900
    sb = CFG["singbox"]["binary"]
    out: dict = {"updated": int(time.time()), "nodes": {}}
    # 补测单个节点时不能把已有结果冲掉：v4 渠道的分配依赖全表。
    if merge:
        out["nodes"].update(load_colo_map())
    for n in pool:
        cfg = build_line_config(profile, port, n, _wg()["peer_v4"], family="v4")
        cfg["log"]["level"] = "error"
        fields: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="wgsurvey-") as td:
            path = os.path.join(td, "c.json")
            write_json(path, cfg, mode=0o600, indent=2)
            proc = subprocess.Popen([sb, "run", "-c", path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                deadline = time.time() + timeout
                while time.time() < deadline:
                    fields = _trace_fields(port, 5.0)
                    if fields.get("warp") == "on":
                        break
                    time.sleep(1.0)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        ok = fields.get("warp") == "on" and bool(fields.get("ip"))
        out["nodes"][n["tag"]] = {
            "colo": fields.get("colo"), "loc": fields.get("loc"),
            "v4": fields.get("ip"), "ok": ok,
        }
        print(f"{n['tag'][:44]:44s} {'OK' if ok else 'FAIL':4s} "
              f"{fields.get('colo') or '-':4s} {fields.get('loc') or '-':3s} "
              f"{fields.get('ip') or '-'}", file=sys.stderr, flush=True)
    write_json(str(colo_map_path()), out, mode=0o600, indent=2)
    cols = {(v.get("colo"), v.get("v4")) for v in out["nodes"].values() if v.get("ok")}
    print(f"落地组 {len(cols)} 个，覆盖节点 {sum(1 for v in out['nodes'].values() if v.get('ok'))} 个",
          file=sys.stderr)
    return out


# ---------------------------------------------------------------- 编排

def _read_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {"lines": {}}


def line_nodes() -> dict[str, str]:
    """`{线路 tag: 该线当前落地节点}`，取自已落盘的线路台账。

    给夜间白名单用：块说「哪个 colo 还值得刷」，这份台账说「哪条线现在落在
    那个 colo」。台账缺失时返回空 dict —— 调用方据此退回配置里的静态名单，
    而不是猜一个「全保留」。
    """
    out = {}
    for tag, line in (_read_state().get("lines") or {}).items():
        if isinstance(line, dict) and line.get("node"):
            out[tag] = line["node"]
    return out


def _rotation_for(home: dict, pool: list[dict], size: int) -> list[str]:
    """后备顺序：先别的出口组，再同组。

    同组切换大概率拿到同一出口地址（v4 由 colo 决定），所以只在没有跨组
    候选时才把同组排进末尾。
    """
    hk = region_key(home)
    same = sorted((n for n in pool if region_key(n) == hk), key=lambda n: n["tag"])
    diff = sorted((n for n in pool if region_key(n) != hk), key=lambda n: n["tag"])
    tail = [n["tag"] for n in diff + same if n["tag"] != home["tag"]]
    return [home["tag"]] + tail[: max(0, size - 1)]


def apply(count: int | None = None, nodes: list[str] | None = None,
          start: int = 0, probe: bool = False, dry_run: bool = False,
          publish: bool = True, delays: dict | None = None) -> dict:
    """按「落地节点 × 渠道模式」生成全部线路。

    每条线 = 一个 WARP 设备 + 一个 sing-box 进程 + 一个 socks 端口。
    同一落地节点上挂 per_node 条渠道（families 的长度），各自带一份在后备
    节点列表里轮转的配置，daemon 侧看到的就是普通成员。

    分配逻辑有两个来源，由 `warp.blocks.enabled` 决定（默认关）：

    | 来源 | 分组依据 | 用途 |
    |---|---|---|
    | `assign_nodes`（旧） | colo 轮转，尽量让每条线落不同主机 | 现网拓扑，保持不变 |
    | `warp_blocks.plan_lines`（新） | 渠道 = (类型, 块, 序号) | 按块开渠道，见 docs/warp-channel-allocation.md |

    两条路径共用后面的注册、写配置、发布步骤 —— 分叉只在这里，因为「选哪个
    节点」是唯一真正不同的地方；把写盘也复制一份，两边就会各自漂移。
    """
    wg = _wg()
    families = list(wg.get("families") or ["v6"])
    per = len(families)
    homes = max(1, int(wg.get("home_nodes") or 1))
    total = count or len(families) * homes
    if total % per:
        raise SystemExit(f"线路数必须是每节点渠道数 {per} 的整数倍（当前 {total}）")
    homes = total // per
    if total <= 0:
        raise SystemExit("线路数为 0：设置 WARP_WG_FAMILIES / WARP_WG_COUNT")

    for i in range(1, total + 1):
        if not (line_dir(i) / "profile.conf").exists():
            if dry_run:
                print(f"[dry-run] 将注册 {line_tag(i)}", file=sys.stderr)
                continue
            print(f"注册 {line_tag(i)} …", file=sys.stderr)
            try:
                register_line(i)
            except (RuntimeError, SystemExit) as e:
                # 半成品目录会让下次 apply 误以为已注册，清掉才能重来。
                shutil.rmtree(line_dir(i), ignore_errors=True)
                done = sum(1 for j in range(1, total + 1)
                           if (line_dir(j) / "profile.conf").exists())
                raise SystemExit(
                    f"✗ {line_tag(i)} 注册失败: {e}\n"
                    f"  已就绪 {done} 条线，可先按 --count {done} 生成配置；"
                    f" 新设备的注册窗口过去后再补齐。") from e

    pool = udp_capable(load_nodes(), probe=probe)
    by_tag = {n["tag"]: n for n in pool}
    # 实测判定不可用的节点不参与落地选择：拿死节点当 home，这条线就永远
    # 连不上（16 个 unamecf 系节点此前一直占着 WARP-WG-1~4 的 home）。
    colo = load_colo_map()
    usable = [n for n in pool
              if (colo.get(n["tag"]) or {}).get("ok") is not False]
    if not usable:
        # 原来写成 `... or pool`：一个能用的都没有时静默退回全部。那等于让 WARP
        # 拿死节点当 home、线永远连不上，而日志上一片正常。兜底可以，但要喊。
        print("  ⚠ 台账里所有节点都被标死，退回全部节点当落地点候选"
              "（这些线大概率连不上）", file=sys.stderr)
        usable = pool
    rot_size = max(1, int(wg.get("rotation_size") or 8))
    plan = None
    if blocks.enabled(CFG) and not nodes:
        # 渠道清单已按块排好序，这里只截断到线路数：多出来的渠道是备选池，
        # 不是「该多开几条线」—— 线路数由 WARP_WG_COUNT 决定，不由块数决定。
        # 块表里混了两种承载：WG（UDP 经节点）和 H2（TCP 经节点）。`udp_capable`
        # 对后者必然判 False，而 H2 线由 deploy/render_h2_lines.py 渲染，不归这里。
        # 不把 H2 渠道剔出去，整次 apply 会直接 SystemExit（实测：udp 缓存一刷新，
        # 29 条线一条也铺不出来）。远端首次部署时缓存一定是刚测的，这不是边角情况。
        # 同理剔掉「块表里有、但本次 UDP 池里没有」的节点：那是缓存与实测表
        # 不同步，不该让整次部署失败。
        nodes_map = blocks.load_map()
        chans = blocks.channels(CFG, delays=delays)
        wg_chans = [c for c in chans
                    if (nodes_map.get(c["node"]) or {}).get("carrier") != "h2"
                    and c["node"] in by_tag]
        h2_n = sum(1 for c in chans
                   if (nodes_map.get(c["node"]) or {}).get("carrier") == "h2")
        if h2_n:
            print(f"  跳过 {h2_n} 条 H2 渠道（不占 WG 线位，见 deploy/render_h2_lines.py）",
                  file=sys.stderr)
        # 先展开，再按**实际**条数建线：`plan_lines` 会因「同一节点不当两条线的
        # home」而少给几条（pin 让同一节点出现在两个块里），拿旧的 total 建线
        # 会 IndexError。线路数是结果，不是输入。
        candidate = blocks.plan_lines(
            wg_chans, min(total, len(wg_chans)), rotation_size=rot_size)
        if len(candidate) < total:
            print(f"  ⚠ WG 渠道去重后只有 {len(candidate)} 条（要 {total} 条）；"
                  f"按实际条数建线", file=sys.stderr)
        if candidate:
            plan = candidate
            total = len(plan)
            print(f"  块模型接管分配：{len(plan)} 条渠道 → {total} 条线路",
                  file=sys.stderr)

    # 选了块模型就不再跑 `assign_nodes`：那是另一套选点逻辑，它算出来的
    # `home_nodes` 一条也不会被用到，但它会先在「可用节点不足」时 SystemExit
    # —— 明明块表够用，却被一条不生效的路径拦住。
    home_nodes: list[dict] = []
    if plan is None:
        if nodes:
            missing = [t for t in nodes if t not in by_tag]
            if missing:
                raise SystemExit(f"节点不在当前订阅里或 UDP 不可用: {missing}")
            # 显式 --nodes 原本绕过 ok 过滤：指定一个实测死的节点会被照单收下，
            # home 直接写进线配置、那条线从此连不上。照用但要说一声。
            dead = [t for t in nodes if (colo.get(t) or {}).get("ok") is False]
            if dead:
                print(f"  ⚠ 显式指定的节点里有台账判死的 {dead}；照用，线可能连不上",
                      file=sys.stderr)
            home_nodes = [by_tag[t] for t in nodes][:homes]
            if len(home_nodes) < homes:
                raise SystemExit(f"落地节点不足：需要 {homes} 个，给了 {len(home_nodes)} 个")
        else:
            # 按实测 colo 分组：v4 出口只由 colo 决定，同 colo 多挂 v4 线只会
            # 抢同一个地址池（实测同节点重启即在 .211/.243 间跳，与节点无关）。
            known = {str(v.get("colo")) for v in colo.values() if v.get("colo")}
            home_nodes = assign_nodes(
                usable, homes, start, key=region_key,
                rank=lambda k: 0 if k in known else 1)
    state = {"lines": {}, "updated": int(time.time())}
    for i in range(1, total + 1):
        group = (i - 1) // per
        if plan:
            p = plan[i - 1]
            home = by_tag[p["node"]]
            family = p["family"]
            rotation = p["rotation"]
        else:
            home = home_nodes[group]
            rotation = _rotation_for(home, usable, rot_size)
            family = families[(i - 1) % per]
        # 同组的兄弟线错开后备顺序，避免三条线同时跳去同一个节点。
        off = (i - 1) % per
        head, tail = rotation[0], rotation[1:]
        if tail:
            off %= len(tail)
            tail = tail[off:] + tail[:off]
        state["lines"][line_tag(i)] = {
            "unit": unit_name(i),
            "port": wg["base_port"] + i - 1,
            "node": home["tag"],
            "family": family,
            "home": home["tag"],
            "rotation": [head] + tail,
            # 写成当前时刻而不是 0：daemon 的同块轮回守护用它当「刚动过，先别
            # 判死」的闸门，0 会让刚 apply 出来的线立刻进入判死计数。
            "rebind_ts": int(time.time()),
        }
        if dry_run:
            print(f"[dry-run] {line_tag(i)} -> {home['tag']} ({family}, "
                  f"port {wg['base_port'] + i - 1})")
            continue
        write_line(i, home, family=family)
    if dry_run:
        return state

    # 未启用的线不能进主配置：warp-outbound.json 是线上 sing-box 的唯一出站
    # 数据源，下一轮订阅刷新就会把这些还没跑起来的端口挂进路由。
    if publish:
        refresh_main_outbounds()
    write_json(str(state_path()), state, mode=0o600, indent=2)
    return state


def rebind(i: int, probe: bool = False) -> str | None:
    """把线切到后备列表里的下一个节点，只写配置、不重启。

    重启留给调用方：daemon 要按自己的并发锁与在途连接判定来重启，CLI 的
    rotate 则立刻重启。两处共用这一个「换节点」原语，避免两份选点逻辑漂移。
    """
    st = _read_state()
    line = st.setdefault("lines", {}).setdefault(line_tag(i), {
        "unit": unit_name(i), "port": _wg()["base_port"] + i - 1,
        "family": line_family(i), "rotation": [],
    })
    pool = {n["tag"]: n for n in udp_capable(load_nodes())}
    rot = [t for t in (line.get("rotation") or []) if t in pool]
    cur = line.get("node")
    if cur in rot:
        k = rot.index(cur)
        rot = rot[k + 1:] + rot[:k]
    for tag in rot:
        if tag == cur:
            continue
        if probe and not probe_node_tcp(pool[tag]):
            continue
        # family 用**当前配置声明的值**，不用状态表里的 `line["family"]`：
        # 后者是上一次 `apply()` 的快照，改过拓扑（如 `WARP_WG_FAMILIES`
        # 从 `v4,v6,v6,v6` 变成 `v6`）之后它就是旧世代残留。实测远端
        # 2026-11-05：状态表记 WG-1/2/3 为 v4，而 `.env` 是 `v6` —— rebind
        # 按状态表重写，把 line2/line3 写回了 `prefer_ipv4`，v4 出口只由
        # colo 决定 ⇒ 三条线撞成同一个 `104.28.205.117`。
        # `line_family(i)` 与 `apply()` 的取值口径完全一致。
        write_line(i, pool[tag], family=line_family(i))
        line["node"] = tag
        line["rebind_ts"] = int(time.time())
        st["updated"] = int(time.time())
        write_json(str(state_path()), st, mode=0o600, indent=2)
        return tag
    return None


def validate_bindings(probe: bool = False) -> list[str]:
    """订阅刷新后调用：绑定节点已下架的线换到后备节点。

    线路配置里内嵌了节点 outbound 的副本，订阅换 IP/改名后旧副本会永久
    连不上，而 daemon 只会不停重启一条注定失败的线。
    """
    pool = {n["tag"] for n in udp_capable(load_nodes())}
    st = _read_state()
    moved: list[str] = []
    for i in range(1, _wg()["count"] + 1):
        line = (st.get("lines") or {}).get(line_tag(i)) or {}
        if line.get("node") in pool:
            continue
        if rebind(i, probe=probe):
            moved.append(line_tag(i))
    return moved


def rotate(i: int, probe: bool = False, dry_run: bool = False) -> dict:
    """换节点并重启该线 —— v4 渠道换地址只能靠这个，v6 渠道用不上。"""
    st = _read_state()
    tag = line_tag(i)
    line = (st.get("lines") or {}).get(tag) or {}
    cur = line.get("node")
    if dry_run:
        rot = line.get("rotation") or []
        nxt = rot[(rot.index(cur) + 1) % len(rot)] if cur in rot and rot else "-"
        print(f"[dry-run] {tag}: {cur} -> {nxt}")
        return st
    new = rebind(i, probe=probe)
    if not new:
        raise SystemExit("没有可用节点可切换")
    _run(["systemctl", "restart", unit_name(i)])
    print(f"{tag}: {cur} -> {new}（已重启）")
    return st


def status() -> None:
    st = json.loads(state_path().read_text()) if state_path().exists() else {"lines": {}}
    # 状态文件才是权威：管理机上不该为了看一眼线路而导出 WARP_WG_*。
    idxs = (sorted(int(t.rsplit("-", 1)[-1]) for t in st["lines"])
            or range(1, _wg()["count"] + 1))
    for i in idxs:
        tag, unit = line_tag(i), unit_name(i)
        port = _wg()["base_port"] + i - 1
        active = subprocess.run(["systemctl", "is-active", unit],
                                capture_output=True, text=True).stdout.strip() or "unknown"
        line = st.get("lines", {}).get(tag) or {}
        node = line.get("node", "-")
        family = line.get("family") or line_family(i)
        home = line.get("home", "-")
        print(f"{tag:12} port={port} unit={active:9} {family:2} node={node} home={home}")


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="WARP-over-proxy 线路管理")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register", help="注册 WARP 设备并生成 profile")
    p.add_argument("--count", type=int)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("apply", help="生成线路配置与主配置出站清单")
    p.add_argument("--count", type=int,
                   help="总线路数（必须是每节点渠道数的整数倍）")
    p.add_argument("--nodes", nargs="*",
                   help="显式指定落地节点 tag，一个节点下挂 per_node 条渠道")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--probe", action="store_true", help="先探测节点 UDP 能力")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-publish", action="store_true",
                   help="不写 .warp/warp-outbound.json（线路还没起时用）")

    p = sub.add_parser("rotate", help="换节点并重启某条线（v4 渠道换地址用）")
    p.add_argument("index", type=int)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("rebind", help="只换节点写配置，不重启（daemon 调用）")
    p.add_argument("index", type=int)
    p.add_argument("--probe", action="store_true")

    sub.add_parser("validate", help="订阅刷新后校验绑定，失效的自动换节点")

    p = sub.add_parser("survey", help="实测每个节点的 WARP 落地 colo 与 v4 出口")
    p.add_argument("--nodes", nargs="*", help="只补测这些节点，结果并入现有地图")
    sub.add_parser("status", help="查看线路与单元状态")
    args = ap.parse_args()

    if args.cmd == "register":
        for i in range(1, (args.count or _wg()["count"]) + 1):
            register_line(i, force=args.force)
            print(f"registered {line_tag(i)}")
    elif args.cmd == "apply":
        st = apply(count=args.count, nodes=args.nodes, start=args.start,
                   probe=args.probe, dry_run=args.dry_run,
                   publish=not args.no_publish)
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif args.cmd == "rotate":
        rotate(args.index, probe=args.probe, dry_run=args.dry_run)
    elif args.cmd == "rebind":
        tag = rebind(args.index, probe=args.probe)
        print(tag or "无可用节点")
        if not tag:
            raise SystemExit(1)
    elif args.cmd == "survey":
        survey(only=args.nodes)
    elif args.cmd == "validate":
        moved = validate_bindings(probe=True)
        print("已换节点:", moved or "无")
    elif args.cmd == "status":
        status()


if __name__ == "__main__":
    main()
