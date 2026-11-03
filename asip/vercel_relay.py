"""Vercel 中继线路：把 9router 的出口挪到 Vercel 的 egress 上。

为什么要有第三条地址空间
------------------------
asip 现在只有两种出口：sing-box 里的节点池，和 20 条 WARP 隧道。两者都受同
一个上游按 IP 记账 —— 一个地址用脏了就得换下一个。WARP 的 v6 池近乎无上界，
但它终究是 Cloudflare 的段：上游一旦对整段做信誉，20 条隧道会一起变脏，那时
换线只是在同一个段里挪位置。

中继提供的是另一个段。9router 早就认这套协议（`x-relay-target` + `x-relay-path`）：
proxyPools 里 `type` 属于 RELAY_KINDS 的条目会被 proxyAwareFetch 挑出来，请求打到
relay 部署上，由那家平台的出口发往上游。

**同一份协议，多个平台。** 9router 支持 vercel / cloudflare / deno / netlify 四种，
差别只在 `proxyPools.data.type` 这个字符串 —— headers 完全一样。所以这条链路上
「换一家平台」的代价只在于部署脚本，不在于接入层；台账里每条 relay 记一个 `kind`，
同步时写进对应的 type。

诚实地说清一件事：Vercel 与 Netlify 的出口都落在 AWS，**同一段**。多接一家不等于
多一个段，它换的是「服务商」和「地址池的随机性」，不是 ASN。要真正跳出 AWS，得看
Cloudflare Workers / Deno Deploy 那两家；本模块留了 kind 的口子，部署脚本没做。

本模块只做三件事，且都不 import config / daemon（调用方把路径传进来）：

1. 把 relay 协议说清楚 —— `relay_headers` / `probe`；
2. 记台账 —— `.vercel_relay.json`：每条 relay 的出口 IP、最近成功、被烧到几时；
3. 把台账同步进 9router 的 SQLite —— proxyPools 行 + providerStrategies。

选路沿用 WARP 那条思路：**没被烧过的地址优先，窗口外用过的次之，其余兜底**。
区别在于 Vercel 的出口地址本来就每次可能不同，所以这里的「地址」是上一次观测
到的那个值，不是承诺；台账记的是证据，不是配额。
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from . import atomic_io
from .http import request_json

# 9router 的 relay 协议：目标主机放在头里，路径放在另一个头里。
# 两个头都必须小写 —— Node 的 Headers 会归一化，但代理/中间层不保证。
TIMING_HEADER = "x-relay-timing"
RELAY_TYPE = "vercel"
# 9router 认的四种中继平台（connectionProxy.js:65）。headers 一样，只有 type 不同。
RELAY_KINDS = ("vercel", "cloudflare", "deno", "netlify")
TARGET_HEADER = "x-relay-target"
PATH_HEADER = "x-relay-path"
DEFAULT_PROBE_TARGET = "https://api.ipify.org"
DEFAULT_GUARD_SEC = 86400
DEFAULT_BLOCKED_TTL_SEC = 86400
LEDGER_VERSION = 1

# 被烧的两类原因各自保留多久。与 warp 的语义对齐：403 是地址信誉，429 是配额。
_BURN_TTL = {
    "abuse403": DEFAULT_BLOCKED_TTL_SEC,
    "rate429": 60,
}


@dataclass(frozen=True)
class ProbeResult:
    """一次 relay 探测的结果。`ip is None` 时 `error` 一定有值。"""

    url: str
    ip: str | None = None
    status: int | None = None
    latency_ms: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.ip is not None


def normalize_relay_url(url: Any) -> str | None:
    """只接受 http(s) 且带主机名的地址；去掉末尾斜杠。

    拒绝其它协议是刻意的：这个字符串最终会被 9router 写进 proxyUrl，一个
    `file:` 或空主机名会在 9router 侧变成「每次都直连」，而台账仍显示正常。
    """
    if not isinstance(url, str):
        return None
    text = url.strip().rstrip("/")
    if not text or "://" not in text:
        return None
    scheme, _, rest = text.partition("://")
    if scheme.lower() not in ("http", "https"):
        return None
    host = rest.split("/", 1)[0]
    if not host or host.startswith(":"):
        return None
    return text


def relay_headers(target: str, path: str = "/") -> dict[str, str]:
    """9router 侧认的请求头。`path` 为空时按 `/` 处理。"""
    return {TARGET_HEADER: str(target), PATH_HEADER: str(path or "/")}


def _kind(value: Any) -> str:
    """把任意输入收窄成合法的中继类型；不认识的一律当 vercel。

    宁可写错 type 让 9router 当成普通代理，也不能让一条读不懂的字符串进库 ——
    后者会让整行 proxyPools 在 proxyConfigFromPool 里走进 else 分支。
    """
    text = str(value or "").strip().lower()
    return text if text in RELAY_KINDS else RELAY_TYPE


def _valid_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None


def probe(url: str, *, target: str = DEFAULT_PROBE_TARGET, path: str = "/",
          timeout: float = 10.0, session: Any = None) -> ProbeResult:
    """经 relay 取一次出口 IP。

    失败分三类，调用方要能区分：`transport`（连不上/超时）、`http_<code>`
    （relay 自己拒绝或 Vercel 报错）、`bad_body`（通了但回的不是地址）。
    把三类塌成一个 `None` 的话，「relay 挂了」和「relay 活着但被上游限流」
    会被记成同一件事，台账就再也解释不了自己。
    """
    norm = normalize_relay_url(url)
    if norm is None:
        return ProbeResult(url=str(url), error="bad_url")
    start = time.monotonic()
    result = request_json(
        "GET", norm + (path or "/"),
        headers=relay_headers(target, path),
        timeout=timeout,
        session=session,
        redact=True,
    )
    latency = int((time.monotonic() - start) * 1000)
    if result.status is None:
        return ProbeResult(url=norm, status=None, latency_ms=latency,
                           error=result.error or "transport")
    if not result.ok:
        return ProbeResult(url=norm, status=result.status, latency_ms=latency,
                           error=f"http_{result.status}")
    body = result.text
    if body is None and result.data is not None:
        body = str(result.data)
    ip = _valid_ip(body or "")
    if ip is None:
        return ProbeResult(url=norm, status=result.status, latency_ms=latency,
                           error="bad_body")
    return ProbeResult(url=norm, ip=ip, status=result.status, latency_ms=latency)


# ─────────────────────────── 台账 ───────────────────────────

def timing_probe(url: str, *, target: str = DEFAULT_PROBE_TARGET, path: str = "/",
                 timeout: float = 25.0, session: Any = None) -> dict[str, Any]:
    """让 relay 自己报账：函数 -> 上游那一段的耗时。

    为什么非要在函数里量：从外部算「经中继多花多少」时，本机网络抖动实测 1–2.5s，
    比要测的那点开销大一个量级 —— 差值能算出负数。让函数用 Date.now() 自己记，
    「本地 -> Vercel」这一段就彻底不参与计算了。

    返回的是**测量结果**，不是探针结果；失败时给 ok=False，不抛异常。
    """
    norm = normalize_relay_url(url)
    if norm is None:
        return {"ok": False, "error": "bad_url"}
    headers = relay_headers(target, path)
    headers[TIMING_HEADER] = "full"
    result = request_json("GET", norm + (path or "/"), headers=headers,
                          timeout=timeout, session=session, redact=True)
    if result.status is None:
        return {"ok": False, "error": result.error or "transport"}
    if not result.ok:
        return {"ok": False, "error": f"http_{result.status}", "status": result.status}
    data = result.data
    if not isinstance(data, dict):
        # 老版本函数不认识 x-relay-timing：它会把上游正文透传回来，这里拿到的不是账本。
        return {"ok": False, "error": "no_timing_support"}
    return {
        "ok": True,
        "status": _as_int(data.get("status")),
        "region": str(data.get("region") or ""),
        "runtime": str(data.get("runtime") or ""),
        "fetch_ms": _as_int(data.get("fetch_ms")),
        "first_chunk_ms": _as_int(data.get("first_chunk_ms")) or None,
        "total_ms": _as_int(data.get("total_ms")),
        "bytes": _as_int(data.get("bytes")),
    }


def record_timing(ledger: dict, relay_id: str, sample: dict,
                  now: float | None = None) -> None:
    """把一次自报耗时写进台账（只存 fetch_ms，其余留给 bench 当场打印）。"""
    entry = ledger["relays"].get(relay_id)
    if entry is None or not sample.get("ok"):
        return
    if sample.get("fetch_ms"):
        entry["fetch_ms"] = _as_int(sample["fetch_ms"])
        entry["timing_ts"] = now if now is not None else time.time()


def ip_changed(prev: Any, new: Any) -> bool:
    """重部署到底换没换地址 —— 只能拿前后两次观测比。

    实测重部署不一定换 IP（2026-10-29 五次里换了三次），所以这里不做任何假设：
    新地址为空（没探到）一律算没换。
    """
    old = _valid_ip(str(prev or ""))
    fresh = _valid_ip(str(new or ""))
    return bool(fresh) and fresh != old


def refresh_until_new_ip(ledger: dict, relay_id: str, redeploy: Any,
                         probe_fn: Any = None, *, attempts: int = 3,
                         now: float | None = None) -> dict:
    """重部署，然后**用现有的探针**看换没换；没换就再来。

    探测不另写一套：probe / record_probe 就是现成的，这里只是把它们串进循环里，
    顺便把每次的观测留在台账（prev_ip）里 —— 换没换是两次观测的差，不是印象。

    redeploy 由调用方注入（CLI 传 subprocess，单测传个假的），所以这个循环
    既能在命令行用，也能被以后的 daemon 复用，还不用联网就能测。
    """
    entry = ledger["relays"].get(relay_id)
    if entry is None:
        return {"ok": False, "error": "unknown_relay", "tries": []}
    probe_fn = probe_fn or probe
    old = entry.get("ip")
    tries: list[dict[str, Any]] = []
    for i in range(max(1, attempts)):
        redeploy()
        result = probe_fn(entry["url"])
        record_probe(ledger, relay_id, result, now=now)
        changed = ip_changed(old, result.ip)
        tries.append({"attempt": i + 1, "ip": result.ip,
                      "error": result.error, "changed": changed})
        if changed:
            return {"ok": True, "old": old, "new": result.ip,
                    "attempts": i + 1, "tries": tries}
    return {"ok": False, "old": old, "new": entry.get("ip"),
            "attempts": len(tries), "tries": tries}


def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "relays": {}, "ip_used": {},
            "updated_at": 0}


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _normalize_entry(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    url = normalize_relay_url(raw.get("url"))
    if url is None:
        return None
    runtime = str(raw.get("runtime") or "edge").strip().lower()
    if runtime not in ("edge", "node"):
        runtime = "edge"
    return {
        "url": url,
        "name": str(raw.get("name") or ""),
        "kind": _kind(raw.get("kind")),
        # 可控性的三块料：怎么部署的（runtime/region）、能不能用（enabled）、
        # 换 IP 时上一版是什么（prev_ip 只用于对账，不参与选路）。
        "runtime": runtime,
        "region": str(raw.get("region") or "").strip(),
        "project": str(raw.get("project") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "asn": str(raw.get("asn") or "").strip(),
        "fetch_ms": _as_int(raw.get("fetch_ms")) or None,
        "timing_ts": _as_float(raw.get("timing_ts")),
        "prev_ip": _valid_ip(str(raw.get("prev_ip") or "")) or None,
        "pool_id": str(raw.get("pool_id") or ""),
        "ip": _valid_ip(str(raw.get("ip") or "")) or None,
        "last_ok_ts": _as_float(raw.get("last_ok_ts")),
        "last_probe_ts": _as_float(raw.get("last_probe_ts")),
        "latency_ms": _as_int(raw.get("latency_ms")) or None,
        "fail_streak": _as_int(raw.get("fail_streak")),
        "error": str(raw.get("error")) if raw.get("error") else None,
        "burned_until": _as_float(raw.get("burned_until")),
        "burn_kind": str(raw.get("burn_kind") or "") or None,
        "created_at": _as_float(raw.get("created_at")) or time.time(),
    }


def normalize_ledger(raw: Any) -> dict:
    """把磁盘上的任意形状收敛成合法台账。

    坏条目直接丢掉而不是猜：一条读不懂的 relay 记录如果被当成「干净地址」，
    下一轮就会把流量切到它身上 —— 这正是台账存在的反面。
    """
    ledger = empty_ledger()
    if not isinstance(raw, dict):
        return ledger
    relays = raw.get("relays")
    if isinstance(relays, dict):
        for rid, entry in relays.items():
            if not isinstance(rid, str) or not rid:
                continue
            clean = _normalize_entry(entry)
            if clean is not None:
                ledger["relays"][rid] = clean
    used = raw.get("ip_used")
    if isinstance(used, dict):
        for ip, ts in used.items():
            valid = _valid_ip(str(ip))
            if valid is not None:
                ledger["ip_used"][valid] = _as_float(ts)
    ledger["updated_at"] = _as_float(raw.get("updated_at"))
    return ledger


def load(path: str) -> dict:
    return normalize_ledger(atomic_io.read_json(path, None))


def save(path: str, ledger: dict) -> None:
    ledger["version"] = LEDGER_VERSION
    ledger["updated_at"] = time.time()
    atomic_io.write_json(path, ledger, indent=2)


def add_relay(ledger: dict, url: str, *, name: str = "",
              kind: str = RELAY_TYPE,
              runtime: str = "edge", region: str = "", project: str = "",
              relay_id: str | None = None,
              now: float | None = None) -> str | None:
    """登记一条 relay（幂等：同 URL 已存在就复用它的 id）。

    复用时不改走已有的地址，只补元数据 —— 部署信息（runtime/region/project）
    是「怎么重来一遍」的说明书，缺了就没法 refresh。
    """
    norm = normalize_relay_url(url)
    if norm is None:
        return None
    # 先按项目名认领：重部署会换地址（甚至换出带 hash 的部署专用 URL），
    # 但「这一个项目」始终是同一张牌。不认项目的话，重来一次就多一条，
    # 台账会慢慢变成一堆重复项，池子也跟着翻倍。
    alias = f"https://{project}.vercel.app" if project else ""
    if project:
        for rid, entry in ledger["relays"].items():
            if entry.get("project") == project:
                if alias and norm == alias:
                    entry["url"] = alias
                if runtime:
                    entry["runtime"] = runtime if runtime in ("edge", "node") else "edge"
                if region:
                    entry["region"] = region
                if name and not entry.get("name"):
                    entry["name"] = name
                return rid
    for rid, entry in ledger["relays"].items():
        if entry.get("url") == norm:
            if name and not entry.get("name"):
                entry["name"] = name
            if runtime:
                entry["runtime"] = runtime if runtime in ("edge", "node") else "edge"
            if region:
                entry["region"] = region
            if project:
                entry["project"] = project
            return rid
    rid = relay_id or uuid.uuid4().hex[:12]
    ledger["relays"][rid] = {
        "url": norm, "name": name or norm, "kind": _kind(kind),
        "runtime": runtime if runtime in ("edge", "node") else "edge",
        "region": region, "project": project, "enabled": True,
        "asn": "", "prev_ip": None, "fetch_ms": None, "timing_ts": 0.0,
        "pool_id": "", "ip": None,
        "last_ok_ts": 0.0, "last_probe_ts": 0.0, "latency_ms": None,
        "fail_streak": 0, "error": None, "burned_until": 0.0,
        "burn_kind": None,
        "created_at": now if now is not None else time.time(),
    }
    return rid


def record_probe(ledger: dict, relay_id: str, result: ProbeResult,
                 now: float | None = None) -> None:
    now = now if now is not None else time.time()
    entry = ledger["relays"].get(relay_id)
    if entry is None:
        return
    if result.ok and result.ip and entry.get("ip") and entry["ip"] != result.ip:
        entry["prev_ip"] = entry["ip"]
    entry["last_probe_ts"] = now
    if result.ok:
        entry["ip"] = result.ip
        entry["last_ok_ts"] = now
        entry["latency_ms"] = result.latency_ms
        entry["error"] = None
        entry["fail_streak"] = 0
    else:
        entry["error"] = result.error or "probe_failed"
        entry["fail_streak"] = int(entry.get("fail_streak") or 0) + 1


def mark_used(ledger: dict, relay_id: str, now: float | None = None) -> None:
    """记「这个地址真的发过流量」。

    只在真正切过去之后调用。探测也调的话，每 5 分钟一轮的探活会把所有地址
    提前标成「用过」，选路时就没有干净地址可挑了。
    """
    now = now if now is not None else time.time()
    entry = ledger["relays"].get(relay_id)
    if entry is None:
        return
    ip = entry.get("ip")
    if ip:
        ledger["ip_used"][ip] = now


def burn(ledger: dict, relay_id: str, kind: str, *, now: float | None = None,
         ttl_sec: float | None = None) -> float:
    """把一条 relay 的当前地址标脏，返回解禁时刻。"""
    now = now if now is not None else time.time()
    entry = ledger["relays"].get(relay_id)
    if entry is None:
        return 0.0
    ttl = _BURN_TTL.get(kind, DEFAULT_BLOCKED_TTL_SEC) if ttl_sec is None else ttl_sec
    until = now + max(0.0, float(ttl))
    entry["burned_until"] = max(float(entry.get("burned_until") or 0), until)
    entry["burn_kind"] = kind
    ip = entry.get("ip")
    if ip:
        ledger["ip_used"][ip] = now
    return entry["burned_until"]


def prune(ledger: dict, *, now: float | None = None,
          ip_ttl_sec: float = DEFAULT_BLOCKED_TTL_SEC * 2) -> int:
    """清掉过期地址记录，返回清掉的条数。

    `ip_used` 只增不减的话，跑一个月就是几万行 —— 每次选路都要遍历它。
    TTL 取 blocked_ttl 的两倍：窗口外的记录本来就只是「兜底排序」用的。
    """
    now = now if now is not None else time.time()
    stale = [ip for ip, ts in ledger["ip_used"].items()
             if now - _as_float(ts) > ip_ttl_sec]
    for ip in stale:
        ledger["ip_used"].pop(ip, None)
    for entry in ledger["relays"].values():
        if float(entry.get("burned_until") or 0) < now - 3600:
            entry["burned_until"] = 0.0
            entry["burn_kind"] = None
    return len(stale)


def pick(ledger: dict, *, now: float | None = None,
         guard_sec: float = DEFAULT_GUARD_SEC,
         exclude: Iterable[str] = ()) -> str | None:
    """挑一条最该用的 relay，没有可用的返回 `None`。

    分级与 WARP 的 `_warp_ip_rank` 同构：
      0 没被用过的地址（含还没探出 IP 的新 relay）
      1 窗口外用过（越久越好）
      2 窗口内用过（兜底，按最早用的先来）
    被烧且未解禁的直接排除 —— 切回一个还在惩罚期里的地址等于没换。
    """
    now = now if now is not None else time.time()
    skip = set(exclude)
    best: tuple[tuple[int, float, str], str] | None = None
    for rid, entry in ledger["relays"].items():
        if rid in skip or not entry.get("url"):
            continue
        if float(entry.get("burned_until") or 0) > now:
            continue
        ip = entry.get("ip")
        last_ok = _as_float(entry.get("last_ok_ts"))
        if not ip:
            rank = (0, -last_ok)
        else:
            used = _as_float(ledger["ip_used"].get(ip, 0))
            if used <= 0:
                rank = (0, -last_ok)
            elif now - used > guard_sec:
                rank = (1, -used)
            else:
                rank = (2, used)
        key = (rank[0], rank[1], rid)
        if best is None or key < best[0]:
            best = (key, rid)
    return best[1] if best else None


def active_relays(ledger: dict, *, now: float | None = None) -> list[str]:
    """当前可用（没被烧、没在惩罚期）的 relay id 列表。"""
    now = now if now is not None else time.time()
    return [rid for rid, e in ledger["relays"].items()
            if e.get("url") and float(e.get("burned_until") or 0) <= now]


# ─────────────────── 9router SQLite 同步 ───────────────────

POOL_TABLE = "proxyPools"
_POOL_DATA_DEFAULTS = {"noProxy": "", "strictProxy": False}


def pool_payload(name: str, url: str, kind: str = RELAY_TYPE) -> dict:
    """proxyPools.data 的形状，与 9router 的 createProxyPool 一致。"""
    payload = {"name": name, "proxyUrl": url, "type": _kind(kind)}
    payload.update(_POOL_DATA_DEFAULTS)
    return payload


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def _connect(db_path: str) -> sqlite3.Connection:
    # timeout 给足：9router 在 WAL 上写得很勤，撞锁就等，不要立刻报错。
    con = sqlite3.connect(db_path, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


def _require_schema(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (POOL_TABLE,)).fetchone()
    if row is None:
        raise RuntimeError(f"9router 库里没有 {POOL_TABLE} 表（db 路径不对？）")


def sync_pools(db_path: str, ledger: dict, *, dry_run: bool = False,
               now: float | None = None) -> dict[str, str]:
    """确保台账里的每条 relay 在 9router 的 proxyPools 里都有一行。

    返回 `{relay_id: pool_id}`，并写回 ledger 的 `pool_id`。已存在的行按
    `proxyUrl` 认领 —— 同一个 URL 只留一行，否则 9router 的
    `connectionProxyAlternates` 会按 egress id 去重，多出来的行反而让备用出口
    变空（2026-10-27 踩过：三个池共用 7890，备用出口恒为空）。
    """
    now = now if now is not None else time.time()
    stamp = _iso(now)
    mapping: dict[str, str] = {}
    con = _connect(db_path)
    try:
        _require_schema(con)
        rows = {r["id"]: r for r in con.execute(f"SELECT * FROM {POOL_TABLE}")}
        by_url: dict[str, str] = {}
        for rid, row in rows.items():
            try:
                data = json.loads(row["data"] or "{}")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("proxyUrl"):
                by_url[str(data["proxyUrl"])] = rid
        for relay_id, entry in ledger["relays"].items():
            url = entry["url"]
            pool_id = entry.get("pool_id") or by_url.get(url) or uuid.uuid4().hex
            mapping[relay_id] = pool_id
            if dry_run:
                continue
            payload = pool_payload(entry.get("name") or url, url, entry.get("kind"))
            encoded = json.dumps(payload, ensure_ascii=False)
            want_active = 1 if entry.get("enabled", True) else 0
            if pool_id in rows:
                con.execute(
                    f"UPDATE {POOL_TABLE} SET isActive=?, data=?, updatedAt=? WHERE id=?",
                    (want_active, encoded, stamp, pool_id))
            else:
                con.execute(
                    f"INSERT INTO {POOL_TABLE} (id, isActive, testStatus, data, createdAt, updatedAt) "
                    "VALUES (?, ?, 'active', ?, ?, ?)",
                    (pool_id, want_active, encoded, stamp, stamp))
            entry["pool_id"] = pool_id
            by_url[url] = pool_id
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return mapping


def set_pool_active(db_path: str, pool_id: str, active: bool, *,
                    dry_run: bool = False, now: float | None = None) -> bool:
    if not pool_id:
        return False
    if dry_run:
        return True
    now = now if now is not None else time.time()
    con = _connect(db_path)
    try:
        _require_schema(con)
        cur = con.execute(
            f"UPDATE {POOL_TABLE} SET isActive=?, updatedAt=? WHERE id=?",
            (1 if active else 0, _iso(now), pool_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_provider_strategy(db_path: str, provider: str, *,
                          strategy: str = "round-robin",
                          pool_id: str | None = None,
                          dry_run: bool = False) -> dict:
    """把 opencode 这类 free provider 的出口指向中继池。

    `rotateStrategy` 是 9router 每请求现算的（`pickProxyPoolId`），所以写库
    立刻生效，不需要重启 —— 这一点决定了整条链路能不能自动化。
    只动 `providerStrategies[provider]`，别的 provider 一个字节都不碰。
    """
    con = _connect(db_path)
    try:
        row = con.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("9router settings 表里没有 id=1 这一行")
        current = json.loads(row["data"] or "{}")
        strategies = current.get("providerStrategies")
        if not isinstance(strategies, dict):
            strategies = {}
        entry = strategies.get(provider)
        if not isinstance(entry, dict):
            entry = {}
        before = dict(entry)
        entry["rotateStrategy"] = strategy
        if pool_id:
            entry["proxyPoolId"] = pool_id
        strategies[provider] = entry
        current["providerStrategies"] = strategies
        if dry_run:
            return {"before": before, "after": dict(entry), "written": False}
        con.execute("UPDATE settings SET data=? WHERE id=1",
                    (json.dumps(current, ensure_ascii=False),))
        con.commit()
        return {"before": before, "after": dict(entry), "written": True}
    finally:
        con.close()


def read_provider_strategy(db_path: str, provider: str) -> dict:
    con = _connect(db_path)
    try:
        row = con.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row is None:
            return {}
        data = json.loads(row["data"] or "{}")
        strategies = data.get("providerStrategies")
        entry = (strategies or {}).get(provider) if isinstance(strategies, dict) else None
        return dict(entry) if isinstance(entry, dict) else {}
    finally:
        con.close()


def list_pools(db_path: str) -> list[dict]:
    """只读：把 proxyPools 里属于中继的行摊平成 dict 列表。"""
    con = _connect(db_path)
    try:
        _require_schema(con)
        out = []
        for row in con.execute(f"SELECT * FROM {POOL_TABLE}"):
            try:
                data = json.loads(row["data"] or "{}")
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("type") not in RELAY_KINDS:
                continue
            out.append({"id": row["id"], "active": bool(row["isActive"]),
                        "kind": data.get("type"),
                        "url": data.get("proxyUrl"), "name": data.get("name")})
        return out
    finally:
        con.close()


def set_enabled(ledger: dict, relay_id: str, enabled: bool) -> bool:
    """开/关一条 relay。关掉不等于删掉 —— 地址、region、上次的 IP 都留着，
    下次还能原地打开（关掉期间池是 isActive=0，9router 不会再挑它）。"""
    entry = ledger["relays"].get(relay_id)
    if entry is None:
        return False
    entry["enabled"] = bool(enabled)
    return True


def refresh_url(ledger: dict, relay_id: str) -> str | None:
    """返回这条 relay 的「同一个地址」—— 部署脚本用项目名重来时认这个。

    项目名优先取 entry["project"]；老条目没有就从未部署 URL 里反推
    （https://<project>.vercel.app 或 https://<project>-<hash>-oahcom.vercel.app）。
    """
    entry = ledger["relays"].get(relay_id)
    if entry is None:
        return None
    project = entry.get("project") or ""
    if project:
        return project
    host = normalize_relay_url(entry["url"]) or ""
    host = host.split("//", 1)[-1].split("/", 1)[0]
    if not host.endswith(".vercel.app"):
        return None
    # 别名就是项目名；部署专用 URL（<project>-<hash>-<team>.vercel.app）在台账里
    # 早就统一换成了别名，这里只兜底「还是老 URL」的情况。
    return host[: -len(".vercel.app")]
