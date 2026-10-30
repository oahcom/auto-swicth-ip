"""Vercel 中继的单元测试（T1–T16）。

不碰生产库、不访问网络：探针用 monkeypatch 换掉 request_json，9router 的库用临时
sqlite 按同样表结构建出来。

两种跑法都行：

    python3 -m pytest test_vercel_relay.py -q     # 有 pytest 时的标准跑法
    python3 test_vercel_relay.py                  # 没有 pytest 时的自跑（本机现状）
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import tempfile
from unittest import mock

from asip import vercel_relay as vr
from asip.http import HttpResult

NOW = 1_800_000_000.0


def _db(tmp_path):
    path = os.path.join(tmp_path, "data.sqlite")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE proxyPools (id TEXT PRIMARY KEY, isActive INTEGER DEFAULT 1,"
        " testStatus TEXT DEFAULT 'unknown', data TEXT NOT NULL,"
        " createdAt TEXT, updatedAt TEXT)")
    con.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
    con.execute("INSERT INTO settings (id, data) VALUES (1, ?)",
                (json.dumps({"providerStrategies": {
                    "opencode": {"proxyPoolId": "old-pool", "rotateStrategy": "none"},
                    "other": {"proxyPoolId": "keep-me", "rotateStrategy": "random"}}}),))
    con.commit()
    con.close()
    return path


def _ledger(*urls):
    ledger = vr.empty_ledger()
    for i, url in enumerate(urls):
        vr.add_relay(ledger, url, name="r%d" % i)
    return ledger


def _rows(db, sql):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


# ───────────────────────── T13: URL 归一化 ─────────────────────────

def test_normalize_accepts_http_and_strips_slash():
    assert vr.normalize_relay_url("https://a.vercel.app/") == "https://a.vercel.app"
    assert vr.normalize_relay_url("http://127.0.0.1:8899/") == "http://127.0.0.1:8899"


def test_normalize_rejects_junk():
    for bad in ("", "   ", None, "file:///etc/passwd", "ftp://x.y",
                "https://", "a.vercel.app"):
        assert vr.normalize_relay_url(bad) is None, bad


def test_relay_headers_carry_target_and_path():
    h = vr.relay_headers("https://api.ipify.org", "/x?y=1")
    assert h[vr.TARGET_HEADER] == "https://api.ipify.org"
    assert h[vr.PATH_HEADER] == "/x?y=1"
    assert vr.relay_headers("https://a.b")[vr.PATH_HEADER] == "/"


# ───────────────────────── T3–T5: 探针分类 ─────────────────────────

def test_probe_ok(monkeypatch):
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=200, text="203.0.113.7"))
    r = vr.probe("https://a.vercel.app")
    assert r.ip == "203.0.113.7" and r.error is None and r.status == 200


def test_probe_http_500(monkeypatch):
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=500, text="boom"))
    assert vr.probe("https://a.vercel.app").error == "http_500"


def test_probe_bad_body(monkeypatch):
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=200, text="<html>nope</html>"))
    assert vr.probe("https://a.vercel.app").error == "bad_body"


def test_probe_transport(monkeypatch):
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=None, error="Connection refused"))
    r = vr.probe("https://a.vercel.app")
    assert r.error == "Connection refused" and r.status is None


def test_probe_bad_url_never_calls_out(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("不该发请求")
    monkeypatch.setattr(vr, "request_json", boom)
    assert vr.probe("file:///x").error == "bad_url"


# ───────────────────────── T1–T2: 台账 ─────────────────────────

def test_ledger_roundtrip_and_permissions(tmp_path):
    path = os.path.join(tmp_path, "ledger.json")
    ledger = _ledger("https://a.vercel.app", "https://b.vercel.app")
    vr.save(path, ledger)
    loaded = vr.load(path)
    assert loaded["relays"] == ledger["relays"]
    if os.name != "nt":
        assert (os.stat(path).st_mode & 0o777) == 0o600


def test_corrupt_ledger_degrades_to_empty(tmp_path):
    path = os.path.join(tmp_path, "ledger.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    ledger = vr.load(path)
    assert ledger["relays"] == {} and ledger["ip_used"] == {}


def test_add_relay_is_idempotent():
    ledger = vr.empty_ledger()
    a = vr.add_relay(ledger, "https://a.vercel.app")
    b = vr.add_relay(ledger, "https://a.vercel.app/")   # 归一化后同一条
    assert a == b and len(ledger["relays"]) == 1


# ───────────────────────── T10–T12: 标脏与选路 ─────────────────────────

def test_burn_excludes_relay_until_ttl():
    ledger = _ledger("https://a.vercel.app", "https://b.vercel.app")
    ids = list(ledger["relays"])
    until = vr.burn(ledger, ids[0], "abuse403", now=NOW)
    assert until == NOW + 86400
    assert vr.pick(ledger, now=NOW) == ids[1]
    assert vr.pick(ledger, now=until - 1) == ids[1]
    assert vr.pick(ledger, now=until + 1) in ids


def test_pick_prefers_never_used_addresses():
    ledger = _ledger("https://a.vercel.app", "https://b.vercel.app")
    ids = list(ledger["relays"])
    ledger["relays"][ids[0]]["ip"] = "1.1.1.1"
    ledger["relays"][ids[1]]["ip"] = "2.2.2.2"
    ledger["ip_used"]["2.2.2.2"] = NOW - 10      # 刚用过
    assert vr.pick(ledger, now=NOW) == ids[0]    # 干净的那条先上


def test_pick_returns_none_when_everything_burned():
    ledger = _ledger("https://a.vercel.app")
    for rid in ledger["relays"]:
        vr.burn(ledger, rid, "abuse403", now=NOW)
    assert vr.pick(ledger, now=NOW) is None
    assert vr.active_relays(ledger, now=NOW) == []


def test_prune_drops_stale_ip_records():
    ledger = _ledger("https://a.vercel.app")
    ledger["ip_used"] = {"9.9.9.9": NOW - 3 * 86400, "8.8.8.8": NOW}
    assert vr.prune(ledger, now=NOW) == 1
    assert "8.8.8.8" in ledger["ip_used"]


# ───────────────────────── T6–T7: 池同步 ─────────────────────────

def test_sync_is_idempotent(tmp_path):
    db = _db(tmp_path)
    ledger = _ledger("https://a.vercel.app")
    for _ in range(3):
        vr.sync_pools(db, ledger)
    assert len(_rows(db, "SELECT id FROM proxyPools")) == 1


def test_sync_claims_existing_row_by_url(tmp_path):
    db = _db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO proxyPools (id, isActive, testStatus, data, createdAt, updatedAt)"
                " VALUES ('pre-existing', 1, 'active', ?, 'x', 'x')",
                (json.dumps({"name": "old", "proxyUrl": "https://a.vercel.app",
                             "type": "vercel"}),))
    con.commit()
    con.close()

    ledger = _ledger("https://a.vercel.app")
    mapping = vr.sync_pools(db, ledger)
    assert list(mapping.values()) == ["pre-existing"]
    assert len(_rows(db, "SELECT id FROM proxyPools")) == 1, "同 URL 不能出现第二行"


def test_sync_dry_run_writes_nothing(tmp_path):
    db = _db(tmp_path)
    ledger = _ledger("https://a.vercel.app")
    mapping = vr.sync_pools(db, ledger, dry_run=True)
    assert _rows(db, "SELECT count(*) FROM proxyPools")[0][0] == 0
    # dry-run 连台账也不写：要展示的 id 在返回值里，不靠副作用
    assert len(mapping) == 1
    assert not ledger["relays"][list(ledger["relays"])[0]].get("pool_id")


# ───────────────────────── T8–T9: 策略 ─────────────────────────

def test_strategy_preserves_other_providers(tmp_path):
    db = _db(tmp_path)
    ledger = _ledger("https://a.vercel.app")
    pool = list(vr.sync_pools(db, ledger).values())[0]
    assert vr.set_provider_strategy(db, "opencode", strategy="round-robin",
                                    pool_id=pool)["written"] is True

    data = json.loads(_rows(db, "SELECT data FROM settings WHERE id=1")[0][0])
    assert data["providerStrategies"]["opencode"] == {
        "proxyPoolId": pool, "rotateStrategy": "round-robin"}
    assert data["providerStrategies"]["other"] == {
        "proxyPoolId": "keep-me", "rotateStrategy": "random"}


def test_strategy_dry_run_reports_but_does_not_write(tmp_path):
    db = _db(tmp_path)
    assert vr.set_provider_strategy(db, "opencode", dry_run=True)["written"] is False
    assert vr.read_provider_strategy(db, "opencode")["rotateStrategy"] == "none"


def test_pool_payload_shape_matches_9router():
    p = vr.pool_payload("n", "https://a.vercel.app")
    assert p["type"] == "vercel" and p["proxyUrl"] == "https://a.vercel.app"
    assert set(p) == {"name", "proxyUrl", "type", "noProxy", "strictProxy"}


# ───────────────────────── 多平台（netlify） ─────────────────────────

def test_add_relay_records_kind():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://a.netlify.app", kind="netlify")
    assert ledger["relays"][rid]["kind"] == "netlify"


def test_unknown_kind_falls_back_to_vercel():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://a.example", kind="wat")
    assert ledger["relays"][rid]["kind"] == "vercel"


def test_sync_writes_kind_into_pool_type(tmp_path):
    db = _db(tmp_path)
    ledger = vr.empty_ledger()
    vr.add_relay(ledger, "https://a.netlify.app", kind="netlify")
    vr.add_relay(ledger, "https://b.vercel.app", kind="vercel")
    vr.sync_pools(db, ledger)
    types = sorted(json.loads(r[0])["type"]
                   for r in _rows(db, "SELECT data FROM proxyPools"))
    assert types == ["netlify", "vercel"]


def test_list_pools_includes_all_relay_kinds(tmp_path):
    db = _db(tmp_path)
    ledger = vr.empty_ledger()
    vr.add_relay(ledger, "https://a.netlify.app", kind="netlify")
    vr.sync_pools(db, ledger)
    pools = vr.list_pools(db)
    assert len(pools) == 1 and pools[0]["kind"] == "netlify"


# ───────────────────────── 控制面（打开/关闭/原地换 IP） ─────────────────────────

def test_add_relay_records_deploy_metadata():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://asip-nr-iad1-150410.vercel.app",
                       runtime="node", region="iad1", project="asip-nr-iad1-150410")
    e = ledger["relays"][rid]
    assert (e["runtime"], e["region"], e["project"]) == ("node", "iad1", "asip-nr-iad1-150410")
    assert e["enabled"] is True


def test_add_relay_is_idempotent_but_fills_metadata():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    rid2 = vr.add_relay(ledger, "https://x.vercel.app", runtime="node", region="fra1",
                        project="x")
    assert rid == rid2
    assert ledger["relays"][rid]["runtime"] == "node"
    assert ledger["relays"][rid]["region"] == "fra1"


def test_unknown_runtime_becomes_edge():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://y.vercel.app", runtime="lambda")
    assert ledger["relays"][rid]["runtime"] == "edge"


def test_enabled_survives_roundtrip():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://z.vercel.app")
    assert vr.set_enabled(ledger, rid, False) is True
    again = vr.normalize_ledger(json.loads(json.dumps(ledger)))
    assert again["relays"][rid]["enabled"] is False
    assert vr.set_enabled(ledger, "nope", True) is False


def test_sync_writes_isactive_zero_for_disabled(tmp_path):
    db = _db(tmp_path)
    ledger = vr.empty_ledger()
    on_id = vr.add_relay(ledger, "https://on.vercel.app")
    off_id = vr.add_relay(ledger, "https://off.vercel.app")
    vr.set_enabled(ledger, off_id, False)
    mapping = vr.sync_pools(db, ledger)
    rows = {r[0]: r[1] for r in _rows(db, "SELECT id, isActive FROM proxyPools")}
    assert rows[mapping[on_id]] == 1
    assert rows[mapping[off_id]] == 0


def test_refresh_url_prefers_project_field():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://a-123.vercel.app", project="proj-abc")
    assert vr.refresh_url(ledger, rid) == "proj-abc"


def test_refresh_url_falls_back_to_host():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://asip-nr-hnd1-1.vercel.app")
    assert vr.refresh_url(ledger, rid) == "asip-nr-hnd1-1"


# ───────────────────────── timing：函数自报耗时 ─────────────────────────

def test_timing_probe_parses_report(monkeypatch):
    payload = json.dumps({"region": "iad1", "runtime": "node", "status": 200,
                          "fetch_ms": 412, "first_chunk_ms": 430,
                          "total_ms": 512, "bytes": 14})
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=200, data=json.loads(payload),
                                                   text=None, error=None))
    r = vr.timing_probe("https://x.vercel.app")
    assert r["ok"] is True
    assert (r["fetch_ms"], r["total_ms"], r["bytes"]) == (412, 512, 14)
    assert r["region"] == "iad1" and r["runtime"] == "node"


def test_timing_probe_old_function_is_not_an_error_of_the_network(monkeypatch):
    # 老函数不认识 timing 头，会把上游正文透传回来 —— 这里拿到的是字符串而不是账本
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=200, data=None, text="1.2.3.4",
                                                   error=None))
    r = vr.timing_probe("https://x.vercel.app")
    assert r["ok"] is False and r["error"] == "no_timing_support"


def test_timing_probe_http_error(monkeypatch):
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=502, data=None, text="", error=None))
    assert vr.timing_probe("https://x.vercel.app")["error"] == "http_502"


def test_timing_probe_transport_error(monkeypatch):
    monkeypatch.setattr(vr, "request_json",
                        lambda *a, **k: HttpResult(status=None, data=None, text=None,
                                                   error="timeout"))
    assert vr.timing_probe("https://x.vercel.app")["error"] == "timeout"


def test_timing_probe_bad_url():
    assert vr.timing_probe("not a url")["error"] == "bad_url"


def test_record_timing_writes_fetch_ms():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_timing(ledger, rid, {"ok": True, "fetch_ms": 321}, now=NOW)
    assert ledger["relays"][rid]["fetch_ms"] == 321
    assert ledger["relays"][rid]["timing_ts"] == NOW


def test_record_timing_ignores_failures():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_timing(ledger, rid, {"ok": False, "error": "http_502"})
    assert ledger["relays"][rid]["fetch_ms"] is None


def test_timing_header_is_sent(monkeypatch):
    seen = {}

    def fake(method, url, **kwargs):
        seen.update(kwargs.get("headers") or {})
        return HttpResult(status=200, data={"fetch_ms": 1}, text=None, error=None)

    monkeypatch.setattr(vr, "request_json", fake)
    vr.timing_probe("https://x.vercel.app", target="https://api.ipify.org")
    assert seen.get("x-relay-timing") == "full"
    assert seen.get("x-relay-target") == "https://api.ipify.org"


# ───────────────────────── 换 IP 的判定（拿探针结果对账） ─────────────────────────

def test_record_probe_keeps_previous_ip():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip="1.1.1.1"), now=NOW)
    assert ledger["relays"][rid]["prev_ip"] is None      # 第一次没有「上一版」
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip="2.2.2.2"), now=NOW)
    assert ledger["relays"][rid]["ip"] == "2.2.2.2"
    assert ledger["relays"][rid]["prev_ip"] == "1.1.1.1"


def test_record_probe_ignores_failed_probe_for_prev_ip():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip="1.1.1.1"), now=NOW)
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip=None,
                                                error="timeout"), now=NOW)
    assert ledger["relays"][rid]["prev_ip"] is None
    assert ledger["relays"][rid]["ip"] == "1.1.1.1"


def test_same_ip_does_not_overwrite_prev_ip():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    for ip in ("1.1.1.1", "2.2.2.2", "2.2.2.2"):
        vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip=ip), now=NOW)
    assert ledger["relays"][rid]["prev_ip"] == "1.1.1.1"


# ───────────────────────── refresh 循环（复用探针，不联网） ─────────────────────────

def _seq_probe(ips):
    seq = list(ips)

    def fn(url):
        ip = seq.pop(0)
        if ip is None:
            return vr.ProbeResult(url=url, ip=None, error="timeout")
        return vr.ProbeResult(url=url, ip=ip)
    return fn


def test_refresh_retries_until_probe_sees_new_ip():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip="1.1.1.1"), now=NOW)
    calls = []

    out = vr.refresh_until_new_ip(ledger, rid, lambda: calls.append(1),
                                  _seq_probe(["1.1.1.1", "2.2.2.2"]), attempts=3, now=NOW)
    assert out["ok"] is True
    assert out["attempts"] == 2 and len(calls) == 2
    assert (out["old"], out["new"]) == ("1.1.1.1", "2.2.2.2")
    assert [t["changed"] for t in out["tries"]] == [False, True]
    assert ledger["relays"][rid]["prev_ip"] == "1.1.1.1"


def test_refresh_gives_up_after_attempts():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip="1.1.1.1"), now=NOW)
    calls = []

    out = vr.refresh_until_new_ip(ledger, rid, lambda: calls.append(1),
                                  _seq_probe(["1.1.1.1"] * 3), attempts=3, now=NOW)
    assert out["ok"] is False and out["attempts"] == 3 and len(calls) == 3
    assert ledger["relays"][rid]["ip"] == "1.1.1.1"


def test_refresh_counts_failed_probe_as_not_changed():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://x.vercel.app")
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://x.vercel.app", ip="1.1.1.1"), now=NOW)

    out = vr.refresh_until_new_ip(ledger, rid, lambda: None,
                                  _seq_probe([None, "3.3.3.3"]), attempts=3, now=NOW)
    assert out["ok"] is True and out["attempts"] == 2
    assert out["tries"][0]["error"] == "timeout"


def test_refresh_unknown_relay():
    ledger = vr.empty_ledger()
    assert vr.refresh_until_new_ip(ledger, "nope", lambda: None)["error"] == "unknown_relay"


def test_ip_changed_helper():
    assert vr.ip_changed("1.1.1.1", "2.2.2.2") is True
    assert vr.ip_changed("1.1.1.1", "1.1.1.1") is False
    assert vr.ip_changed(None, "2.2.2.2") is True     # 第一次探到就算「有地址」
    assert vr.ip_changed("1.1.1.1", None) is False
    assert vr.ip_changed("1.1.1.1", "garbage") is False


# ───────────────────────── 同一个项目不能记成两条 ─────────────────────────

def test_same_project_reuses_entry():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://asip-nr-iad1-150410-9b9af418u-oahcom.vercel.app",
                       runtime="node", region="iad1", project="asip-nr-iad1-150410")
    rid2 = vr.add_relay(ledger, "https://asip-nr-iad1-150410.vercel.app",
                        runtime="node", region="iad1", project="asip-nr-iad1-150410")
    assert rid == rid2 and len(ledger["relays"]) == 1
    # 带 hash 的部署 URL 会被别名顶掉
    assert ledger["relays"][rid]["url"] == "https://asip-nr-iad1-150410.vercel.app"


def test_deploy_url_does_not_overwrite_alias():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://p.vercel.app", project="p")
    vr.add_relay(ledger, "https://p-abc123-x-oahcom.vercel.app", project="p")
    assert ledger["relays"][rid]["url"] == "https://p.vercel.app"
    assert len(ledger["relays"]) == 1


def test_different_projects_stay_separate():
    ledger = vr.empty_ledger()
    a = vr.add_relay(ledger, "https://a.vercel.app", project="a")
    b = vr.add_relay(ledger, "https://b.vercel.app", project="b")
    assert a != b and len(ledger["relays"]) == 2


def test_project_reuse_keeps_probed_ip():
    ledger = vr.empty_ledger()
    rid = vr.add_relay(ledger, "https://p.vercel.app", project="p")
    vr.record_probe(ledger, rid, vr.ProbeResult(url="https://p.vercel.app", ip="9.9.9.9"), now=NOW)
    vr.add_relay(ledger, "https://p.vercel.app", project="p", region="iad1")
    assert ledger["relays"][rid]["ip"] == "9.9.9.9"


# ───────────────────────── 自跑（没有 pytest 时） ─────────────────────────

class _MonkeyPatch:
    """pytest 的 monkeypatch 只有 setattr 用得上。"""

    def __init__(self):
        self._patches = []

    def setattr(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        p.start()
        self._patches.append(p)

    def undo(self):
        for p in reversed(self._patches):
            p.stop()
        self._patches.clear()


def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = []
    for name, fn in tests:
        params = inspect.signature(fn).parameters
        with tempfile.TemporaryDirectory() as tmp:
            mp = _MonkeyPatch() if "monkeypatch" in params else None
            kwargs = {}
            if "tmp_path" in params:
                kwargs["tmp_path"] = tmp
            if mp is not None:
                kwargs["monkeypatch"] = mp
            try:
                fn(**kwargs)
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001 - 自跑要看到全部失败
                failed.append((name, exc))
                print("FAIL", name, "->", exc)
            finally:
                if mp is not None:
                    mp.undo()
    print("\n%d/%d 通过" % (len(tests) - len(failed), len(tests)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
