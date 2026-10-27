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
from config import CFG, PROJECT_DIR

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
                 key=None) -> list[dict]:
    """按分组轮转挑节点，尽量让每条线落在不同主机。

    这不是 colo 保证（落地位置由节点出口决定，只能实测），但能避免 N 条线
    全部指向同一台机器的不同端口。
    """
    key = key or node_group
    groups: dict[str, list[dict]] = {}
    for n in nodes:
        groups.setdefault(key(n), []).append(n)
    buckets = [groups[k] for k in sorted(groups)]
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
    node_tag = node["tag"]
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{
            "type": "socks", "tag": "in",
            "listen": _wg()["listen_host"], "listen_port": port,
        }],
        "outbounds": [node],
        # DNS 用本机解析器，和主配置的 dns-default 同一形态。
        # WHY 不做 detour：节点 server 是域名，若 DNS 也 detour 到该节点，
        # 解析节点本身就要先连上节点 —— 环路，sing-box 报
        # "DNS query loopback in transport[dns]"，握手永远起不来。
        "dns": {"servers": [{"type": "local", "tag": "dns-default"}],
                "final": "dns-default",
                "strategy": "prefer_ipv4" if family == "v4" else "prefer_ipv6"},
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
    write_json(str(d / "singbox.json"), cfg, mode=0o600, indent=2)
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


def probe_node_udp(node: dict, timeout: float = 6.0) -> bool:
    """用临时 sing-box 验证该节点是否转发任意端口 UDP。

    判据用 STUN（3478）而不是 53：只有 53 通的节点会骗过探测，而 WG 需要
    2408，属于同一类「非标准端口」能力。
    """
    sb = CFG["singbox"]["binary"]
    with tempfile.TemporaryDirectory(prefix="wgprobe-") as td:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cfg = {
            "log": {"level": "error"},
            "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [node],
            "dns": {"servers": [{"type": "local", "tag": "dns-default"}],
                    "final": "dns-default"},
            "route": {"final": node["tag"],
                      "default_domain_resolver": {"server": "dns-default"}},
        }
        path = os.path.join(td, "probe.json")
        write_json(path, cfg, mode=0o600, indent=2)
        proc = subprocess.Popen([sb, "run", "-c", path],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(20):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                return False
            tid = os.urandom(12)
            pkt = struct.pack("!HHI", 0x0001, 0, 0x2112A442) + tid
            return _socks_udp(port, ("162.159.207.0", 3478), pkt, timeout) is not None
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def udp_cache_path() -> Path:
    return PROJECT / ".warp" / "wg_node_udp.json"


def load_udp_cache() -> dict:
    try:
        return json.loads(udp_cache_path().read_text())
    except (OSError, ValueError):
        return {}


def save_udp_cache(cache: dict) -> None:
    write_json(str(udp_cache_path()), cache, mode=0o600, indent=2)


def udp_capable(nodes: list[dict], probe: bool = False) -> list[dict]:
    """筛掉不转发 UDP 的节点，结果落缓存（默认 6h）。

    24 个节点逐个 STUN 要两分钟，每次 apply 都探一遍不现实。缓存未过期就
    直接复用；没有缓存又没开 --probe 时先放行，由 daemon 的实测失败兜底
    ——宁可多给一条线一次机会，也不要因为探测缺省就把池子砍半。
    """
    ttl = float(_wg().get("udp_cache_ttl_sec") or 6 * 3600)
    cache = load_udp_cache()
    now = time.time()
    out: list[dict] = []
    dirty = False
    for n in nodes:
        tag = n["tag"]
        e = cache.get(tag) or {}
        fresh = now - float(e.get("ts") or 0) < ttl
        if fresh:
            good = bool(e.get("ok"))
        elif probe:
            good = probe_node_udp(n)
            cache[tag] = {"ok": good, "ts": int(now)}
            dirty = True
        else:
            good = True
        if good:
            out.append(n)
    if dirty:
        save_udp_cache(cache)
    return out


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
          publish: bool = True) -> dict:
    """按「落地节点 × 渠道模式」生成全部线路。

    每条线 = 一个 WARP 设备 + 一个 sing-box 进程 + 一个 socks 端口。
    同一落地节点上挂 per_node 条渠道（families 的长度），各自带一份在后备
    节点列表里轮转的配置，daemon 侧看到的就是普通成员。
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
    if nodes:
        missing = [t for t in nodes if t not in by_tag]
        if missing:
            raise SystemExit(f"节点不在当前订阅里或 UDP 不可用: {missing}")
        home_nodes = [by_tag[t] for t in nodes][:homes]
        if len(home_nodes) < homes:
            raise SystemExit(f"落地节点不足：需要 {homes} 个，给了 {len(home_nodes)} 个")
    else:
        # 按实测 colo 分组：v4 出口只由 colo 决定，同 colo 多挂 v4 线只会
        # 抢同一个地址池（实测同节点重启即在 .211/.243 间跳，与节点无关）。
        home_nodes = assign_nodes(pool, homes, start, key=region_key)

    rot_size = max(1, int(wg.get("rotation_size") or 8))
    state = {"lines": {}, "updated": int(time.time())}
    for i in range(1, total + 1):
        group = (i - 1) // per
        home = home_nodes[group]
        rotation = _rotation_for(home, pool, rot_size)
        # 同组的兄弟线错开后备顺序，避免三条线同时跳去同一个节点。
        off = (i - 1) % per
        head, tail = rotation[0], rotation[1:]
        if tail:
            off %= len(tail)
            tail = tail[off:] + tail[:off]
        family = families[(i - 1) % per]
        state["lines"][line_tag(i)] = {
            "unit": unit_name(i),
            "port": wg["base_port"] + i - 1,
            "node": home["tag"],
            "family": family,
            "home": home["tag"],
            "rotation": [head] + tail,
            "rebind_ts": 0,
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
        if probe and not probe_node_udp(pool[tag]):
            continue
        write_line(i, pool[tag], family=line.get("family"))
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
