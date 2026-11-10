import sys, time

sys.path.insert(0, '/home/administrator/dkk-projects/auto-switch-ip')
import pytest

import daemon as d

# 这里只覆盖 _rotate_once / _restart_warp_inplace 自己的语义。换线本体
# （_reconnect_warp_by_table、_restart_warp_line）一律替身。
#
# 本文件原先是「导入期直接跑断言」的脚本，pytest 收集到 0 条 —— `pytest tests/`
# 全绿，daemon.py 改坏了也没人知道。用例必须包成 test_* 才会被判分。


@pytest.fixture(autouse=True)
def _isolate_history(monkeypatch):
    """历史表、出口地址与换线本体，用例之间必须还原/封死。

    历史与 `_ACTIVE_WARP_IP` 是进程级全局，不还原则后面的用例读到前面用例写的
    值，结论由执行顺序决定（本项目踩过同类坑：落池时刻文件未隔离，连跑两次
    结论相反）。

    `_reconnect_warp_by_table` 必须在本文件里一律封死，而不是只在需要它的用例里
    替换：漏替换的用例平时绿只是因为线状态表恰好是空的，走到 no_warm_standby
    就返回。同一次 pytest 运行里只要前面的文件（test_daemon_unit 等）往
    `_WARP_LINE_STATE` 塞过候选，链路就会一路走到 `_switch_warp` → 真发 PUT
    改生产 selector。实测把候选播种进去后 `_rotate_once` 确实发出了
    `PUT /proxies/opencode-out`。
    """
    saved_hist = list(d._WARP_SWITCH_HISTORY)
    saved_active_ip = d._ACTIVE_WARP_IP
    saved_rotate_ts = d._last_rotate_ts
    # 只在 teardown 还原是不够的：别的文件（test_warp_refresh 等）会把它改成
    # "ip-new" 之类的值，本文件后续用例读到的就是那个残留，`_rotate_once` 拿它
    # 当当前出口地址去标脏，结论随执行顺序翻转（实测单跑绿、全量红两条）。
    d._ACTIVE_WARP_IP = None
    # 线表与 selector 快照同属进程级全局：`_rotate_once` 读 `active_warp_tag()`（走
    # 快照）再读线表地址判 was_blocked，两者只要有前文残留，本文件就会看到一条
    # 「有名字、有地址、还在惩罚期」的假当前线 —— was_blocked 为真时代码按设计
    # 跳过 gap 限速直捣 no_warm_standby，`too_frequent` 用例随之翻转。实测全量跑
    # 在 test_daemon_unit / test_warp_refresh 之后必红，单跑恒绿。
    monkeypatch.setattr(d, "_WARP_LINE_STATE", {})
    monkeypatch.setattr(d, "_CLASH_SNAP", {})
    # 槽位表同样是进程级状态：test_slot_failover 会把 6 个槽都标 ok，若这里
    # 不隔离，_fast_switch 会走真实的 slot 秒切，主路径用例记录的出口/地址
    # 全变成槽位里的值（且会尝试连本机 Clash API）。
    monkeypatch.setattr(d, "_SLOT_HEALTH", {})
    monkeypatch.setattr(d, "_MAIN_SLOT_NAME", {"name": None})
    monkeypatch.setattr(d, "_reconnect_warp_by_table",
                        lambda reason, interrupt=True, out=None: False)
    yield
    d._WARP_SWITCH_HISTORY[:] = saved_hist
    d._ACTIVE_WARP_IP = saved_active_ip
    d._last_rotate_ts = saved_rotate_ts


@pytest.fixture
def marked(monkeypatch):
    """`_mark_warp_ip_used` 的记录器：每次调用记 (ip, blocked, kind)。"""
    rec = []
    monkeypatch.setattr(d, "_mark_warp_ip_used",
                        lambda ip, now, blocked=False, kind=None, **kw:
                        rec.append((ip, blocked, kind)))
    return rec


def _fake_switch(reason, interrupt=True, out=None):
    out['tag'] = 'WARP-CF-9'
    out['ip'] = '2a09::new'
    out['switched_at'] = 12345.0
    return True


def _set_line(monkeypatch, tag, ip_of):
    monkeypatch.setattr(d, "active_warp_tag", lambda: tag)
    monkeypatch.setattr(d, "_warp_line_ip", ip_of)


def _hist(*rows):
    d._WARP_SWITCH_HISTORY[:] = list(rows)


# ---- reason 白名单 ----

@pytest.mark.parametrize("raw,want", [
    ('429 quota', 'rate429'), ('rate429', 'rate429'), ('abuse403', 'abuse403'),
    ('403 forbidden', 'abuse403'), ('proactive', 'proactive'),
    ('whatever', 'rate429'), ('', 'rate429'), (None, 'rate429'),
])
def test_normalize_reason(raw, want):
    assert d._normalize_rotate_reason(raw) == want


# ---- 合并 / 限速 / 无备线 ----

def test_coalesced_within_window(monkeypatch):
    monkeypatch.setattr(d, "_last_rotate_ts", time.time())
    r = d._rotate_once('rate429')
    assert r['reason'] == 'coalesced' and r['ok'] is True, r
    assert r['switched'] is False and r['switched_at'] > 0, r


def test_too_frequent_rejected(monkeypatch):
    # 间隔按 `_rotate_gap()` 取，不写死：gap = rate429_guard_sec/线数，9 条线时
    # 6.7s，线数涨到 40 条就压到 1.5s 地板，写死的 2.0 会越过闸门变成假失败。
    gap = d._rotate_gap()
    assert gap > 0.8, gap          # 合并窗 0.8s 必须窄于闸门，否则本用例测的是合并
    monkeypatch.setattr(d, "_last_rotate_ts", time.time() - max(1.0, gap / 2))
    r = d._rotate_once('rate429')
    assert r['reason'] == 'too_frequent' and r['ok'] is False, r


def test_no_warm_standby(monkeypatch):
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    monkeypatch.setattr(d, "_reconnect_warp_by_table",
                        lambda reason, interrupt=True, out=None: False)
    r = d._rotate_once('rate429')
    assert r['reason'] == 'no_warm_standby', r


# ---- 换下来的出口拉黑；主动轮换不拉黑 ----

@pytest.mark.parametrize("reason", ['rate429', 'abuse403'])
def test_switched_marks_old_egress(monkeypatch, marked, reason):
    _set_line(monkeypatch, 'WARP-CF-1', lambda tag: '2a09::old')
    monkeypatch.setattr(d, "_reconnect_warp_by_table", _fake_switch)
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    r = d._rotate_once(reason)
    assert r['ok'] and r['switched'], r
    assert r['tag'] == 'WARP-CF-9' and r['egress'] == '2a09::new', r
    assert r['switched_at'] == 12345.0 and d._last_rotate_ts == 12345.0, r
    assert marked == [('2a09::old', True, reason)], marked


def test_proactive_does_not_mark(monkeypatch, marked):
    _set_line(monkeypatch, 'WARP-CF-1', lambda tag: '2a09::old')
    monkeypatch.setattr(d, "_reconnect_warp_by_table", _fake_switch)
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    r = d._rotate_once('proactive')
    assert r['ok'] and marked == [], marked


# ---- 迟到的 429 按发出时刻回溯，不许记到当前线上 ----
# 生产场景：请求在 WARP-CF-1 上发出，429 迟到几十秒，回来时出口已换成
# WARP-CF-9。旧实现把 WARP-CF-9 记成坏 IP。历史三元组 =
# (生效时刻, 线名, 当时的出口地址)。

def test_late_429_marks_line_at_send_time(monkeypatch, marked):
    _hist((100.0, 'WARP-CF-1', '2a09::old'), (200.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_reconnect_warp_by_table", _fake_switch)
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=150.0)   # 发出时是 CF-1
    assert marked == [('2a09::old', True, 'rate429')], marked


def test_sent_on_current_line_still_marks_it(monkeypatch, marked):
    _hist((100.0, 'WARP-CF-1', '2a09::old'), (200.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=250.0)
    assert marked == [('2a09::new', True, 'rate429')], marked


@pytest.mark.parametrize("started", [50.0, None])
def test_unknown_send_time_falls_back_to_current(monkeypatch, marked, started):
    """早于首条历史 / 调用方没传 → 不知道，退回当前线（旧行为不能变）。"""
    _hist((100.0, 'WARP-CF-1', '2a09::old'), (200.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=started)
    assert marked == [('2a09::new', True, 'rate429')], marked


def test_uses_address_at_send_time_not_current(monkeypatch, marked):
    """那条线在 429 在途期间被换了会话地址：必须用历史里那个地址。

    用此刻的地址等于把 24h 惩罚期记到一个刚换出来的干净地址上。
    """
    _hist((100.0, 'WARP-CF-1', '2a09::while-sent'),
          (200.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9',
              lambda tag: {'WARP-CF-1': '2a09::refreshed-after',
                           'WARP-CF-9': '2a09::new'}.get(tag))
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=150.0)
    assert marked == [('2a09::while-sent', True, 'rate429')], marked


def test_same_line_inplace_reconnect_uses_send_time_address(monkeypatch, marked):
    """同一条线原地重连换了地址：selector 不动、tag 不变，只有地址变了。

    只比 tag 会把这条当「还是当前线」而取新地址 —— 那是迟到的 429 唯一
    没被修到的分支。
    """
    _hist((100.0, 'WARP-CF-1', '2a09::while-sent'))
    _set_line(monkeypatch, 'WARP-CF-1', lambda tag: '2a09::refreshed-after')
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=150.0)
    assert marked == [('2a09::while-sent', True, 'rate429')], \
        f'原地重连后必须记发出时的地址，实际 {marked}'


def test_unknown_address_on_other_line_marks_nothing(monkeypatch, marked):
    """回溯到另一条线但历史里没地址：不许退回标脏当前线。"""
    _hist((100.0, 'WARP-CF-1', None), (200.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=150.0)
    assert marked == [], marked


def test_sent_while_on_pool_marks_nothing(monkeypatch, marked):
    """发出时停在轮换池：那条 429 属于池节点，不归任何隧道。"""
    _hist((100.0, 'WARP-CF-1', '2a09::old'), (200.0, None, None),
          (300.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=250.0)
    assert marked == [], marked


def test_empty_history_falls_back_to_current(monkeypatch, marked):
    _hist()
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=250.0)
    assert marked == [('2a09::new', True, 'rate429')], marked


# ---- 历史写入点：落池与原地重连换址都必须落一条 ----

def test_switch_warp_records_address_from_caller(monkeypatch):
    """`_switch_warp` 记的必须是调用方传进来的新地址，不是线表里的旧地址。

    两个调用方都是「先切 selector、后写线地址表」（daemon.py:5620 与 5330
    都在 `_switch_warp` 之后），所以在 `_switch_warp` 内部读表拿到的必然是
    切换前的陈旧值。迟到的 429 回溯时用的正是这个地址 —— 记错就等于把惩罚期
    记到一个此刻可能已属于别人的地址上。
    """
    _hist()
    monkeypatch.setattr(d, "singbox_api",
                        lambda path, method="GET", body=None, **kw: (200, {}))
    monkeypatch.setattr(d, "_publish_clash_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(d, "_track_offline_refresh", lambda *a, **k: None)
    monkeypatch.setattr(d, "_warp_member", lambda t: {"tag": t})
    monkeypatch.setattr(d, "_current_mode", lambda: ("warp", "WARP-CF"))
    # 线表里是这条线**上一次**的地址
    monkeypatch.setattr(d, "_warp_line_ip", lambda t: "2a09::STALE-OLD")

    assert d._switch_warp("WARP-CF-B", ip="2a09::FRESH-NEW") is True
    assert d._WARP_SWITCH_HISTORY[-1][1:] == ("WARP-CF-B", "2a09::FRESH-NEW"), \
        d._WARP_SWITCH_HISTORY


def test_switch_warp_without_known_address_records_none(monkeypatch):
    """调用方不知道地址时记 None —— 回溯退化成不定罪，比记错地址安全。"""
    _hist()
    monkeypatch.setattr(d, "singbox_api",
                        lambda path, method="GET", body=None, **kw: (200, {}))
    monkeypatch.setattr(d, "_publish_clash_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(d, "_track_offline_refresh", lambda *a, **k: None)
    monkeypatch.setattr(d, "_warp_member", lambda t: {"tag": t})
    monkeypatch.setattr(d, "_current_mode", lambda: ("warp", "WARP-CF"))
    monkeypatch.setattr(d, "_warp_line_ip", lambda t: "2a09::STALE-OLD")

    d._switch_warp("WARP-CF-B")
    assert d._WARP_SWITCH_HISTORY[-1][1:] == ("WARP-CF-B", None), \
        d._WARP_SWITCH_HISTORY


def test_warp_line_at_never_returns_wrong_line_under_truncation(monkeypatch):
    """并发截断下必须要么给对的线、要么说「不知道」，不能给错的线。

    旧写法先 `[h[0] for h in hist]` 算索引、再 `hist[i]` 取值。两次之间若写线程
    执行了 `del hist[:-MAX]`，列表整体前移，同一索引就指向另一条记录 ——
    实测 20 万次读出现 21 次**错误的线**（真值 L100，返回 L101），尾部还 IndexError。

    这里真的开一个写线程不停 append+截断，读线程反复查同一个固定时刻。
    正确实现下答案要么是 L100、要么是 None（快照被换掉了来不及算）；
    出现任何别的线名就是错。
    """
    import threading

    max_n = d._WARP_SWITCH_HISTORY_MAX
    _hist(*[(float(i), f"L{i}", None) for i in range(max_n)])
    target = 100.0

    stop = threading.Event()
    wrong = []

    def writer():
        t = 1000.0
        while not stop.is_set():
            t += 0.001
            d._record_warp_switch_history(f"W{t}", t)
            # 必须让出 GIL：append 与 del 之间若不让出，读线程根本插不进
            # 那两个动作之间，缺陷在测试里就复现不出来（实测不让出时
            # 变异版 20 万次读 0 次错，让出后 3 次即命中）。
            time.sleep(0)

    def reader():
        for _ in range(20000):
            got = d._warp_line_at(target)
            if got is not None and got != ("L100", None):
                wrong.append(got)
                if len(wrong) >= 3:
                    stop.set()
                    return

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    try:
        reader()
    finally:
        stop.set()
        w.join(timeout=5)

    assert wrong == [], f"给出了错误的线：{wrong}"


def test_history_records_pool_switch_as_none():
    _hist()
    d._record_warp_switch_history(None, 123.0)
    assert d._WARP_SWITCH_HISTORY[-1] == (123.0, None, None), d._WARP_SWITCH_HISTORY


def test_history_records_address_snapshot():
    _hist()
    d._record_warp_switch_history('WARP-CF-1', 123.0, '2a09::x')
    assert d._WARP_SWITCH_HISTORY[-1] == (123.0, 'WARP-CF-1', '2a09::x')


def test_history_is_capped():
    _hist()
    for i in range(d._WARP_SWITCH_HISTORY_MAX + 50):
        d._record_warp_switch_history('WARP-CF-1', float(i))
    assert len(d._WARP_SWITCH_HISTORY) == d._WARP_SWITCH_HISTORY_MAX
    assert d._WARP_SWITCH_HISTORY[-1][0] == float(d._WARP_SWITCH_HISTORY_MAX + 49)


def test_restart_inplace_records_new_address(monkeypatch):
    """原地重连换了会话地址后，历史必须跟着换址。

    否则迟到的 429 回溯到这条线时拿到换址前的旧地址，把已经不再承载流量的
    地址记成新坏账。
    """
    _hist((100.0, 'WARP-CF-1', '2a09::old'))
    monkeypatch.setattr(d, "_ACTIVE_WARP_IP", '2a09::old')
    monkeypatch.setattr(d, "_invalidate_warp_line_ip", lambda tag: None)
    monkeypatch.setattr(d, "_restart_warp_line", lambda m, rebind=False: True)
    monkeypatch.setattr(d, "_wait_port_ready", lambda port, t: True)
    monkeypatch.setattr(d, "_probe_refresh_egress", lambda port: '2a09::fresh')
    monkeypatch.setattr(d, "_ip_disqualified", lambda ip, now, guard: False)
    monkeypatch.setattr(d, "_warp_model_usable", lambda m, ip: True)
    monkeypatch.setattr(d, "_begin_segment", lambda ts: None)
    ok = d._restart_warp_inplace(
        {'tag': 'WARP-CF-1', 'port': 1, 'family': 'v6'}, '2a09::old')
    assert ok is True
    assert d._WARP_SWITCH_HISTORY[-1][1:] == ('WARP-CF-1', '2a09::fresh'), \
        d._WARP_SWITCH_HISTORY


# ---- HTTP 层：request_started_at 这个 key 必须真的被读出来 ----
# `_rotate_once` 自己有 20 多条用例，但 handler 是它唯一的线上入口。key 拼错
# 只会静默变成 None，一路退回「标脏当前线」—— 用例全绿、缺陷照旧。所以这里
# 走真实 UDS，把请求体发进去，看回溯用的是哪个时刻。

def _post_rotate(monkeypatch, payload, token="tok"):
    """把请求真的发进 _RotateHandler（UDS），返回 (status, json)。"""
    import http.client, json, os, socket, socketserver, tempfile, threading

    monkeypatch.setattr(d, "_API_TOKEN", token)

    class _Srv(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    sock = tempfile.mktemp(prefix="asip-test-", suffix=".sock")
    srv = _Srv(sock, d._RotateHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("localhost", timeout=10)
        conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.sock.connect(sock)
        conn.request("POST", "/rotate", body=json.dumps(payload),
                     headers={"X-Asip-Token": token,
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        out = (resp.status, json.loads(resp.read()))
        conn.close()
        return out
    finally:
        srv.shutdown()
        srv.server_close()
        try:
            os.unlink(sock)
        except OSError:
            pass


def test_late_429_does_not_lift_rate_limit_on_current_line(monkeypatch):
    """回溯改的是「记到谁头上」，不能顺带解除限速。

    `was_blocked` 的原意是「当前出口已经脏了，再等 gap 也只是继续失败」，所以
    它必须只看**当前出口**。回溯把 `old_ip` 指向另一条线之后，若仍拿它判
    `was_blocked`，就是自证：同一函数下面刚把它标脏，于是恒为真，gap 对每一批
    迟到请求全部失效 —— 实测 4 次调用 4 次换线（对照 2 次），一次风暴换来连续
    换线、地址被成批烧光。

    换线后当前线会变，替身必须跟着变，否则「自我标脏」会让对照组也失灵。
    """
    state = {"cur": "WARP-CF-1", "n": 0,
             "ips": {"WARP-CF-1": "2a09::cur", "WARP-CF-2": "2a09::old"}}

    def _switch(reason, interrupt=True, out=None):
        state["n"] += 1
        tag, ip = f"WARP-NEW{state['n']}", f"2a09::new{state['n']}"
        state["cur"] = tag
        state["ips"][tag] = ip
        out.update(tag=tag, ip=ip, switched_at=time.time())
        return True

    monkeypatch.setattr(d, "active_warp_tag", lambda: state["cur"])
    monkeypatch.setattr(d, "_warp_line_ip", lambda tag: state["ips"].get(tag))
    monkeypatch.setattr(d, "_reconnect_warp_by_table", _switch)
    monkeypatch.setattr(d, "_append_warp_ip_history", lambda *a, **k: None)
    monkeypatch.setattr(d, "_warp_line_invalidate", lambda *a, **k: None)
    monkeypatch.setattr(d, "_probe_cache_forget", lambda *a, **k: None)
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    now = time.time()
    _hist((now - 50, "WARP-CF-2", "2a09::old"),
          (now - 10, "WARP-CF-1", "2a09::cur"))

    # 第一次：回溯到 CF-2，标脏它，换线成功。
    d._rotate_once('rate429', request_started_at=now - 50)
    assert state["cur"] == "WARP-NEW1"
    # 拨过 800ms 的合并窗，但仍在 gap 内：这一步要测的就是 gap。
    d._last_rotate_ts = time.time() - 1.0
    # 第二次仍回溯到同一条已脏的 CF-2：当前出口干净，必须被 gap 拦住。
    r = d._rotate_once('rate429', request_started_at=now - 50)
    assert r['reason'] == 'too_frequent' and not r.get('switched'), r
    assert state["cur"] == "WARP-NEW1", "不该因为历史地址脏就绕过限速"


def test_coalesced_still_marks_the_line_that_was_blocked(monkeypatch, marked):
    """合并窗只该省掉「换线」，不该把证据一起丢掉。

    迟到的 429 是成批涌进来的：第一条换线成功，其余全部命中 800ms 合并窗。
    若合并分支在记账之前就返回，那条真正发出请求的线一条都标不到，下一批
    候选又会把它原样选回来 —— 换线白做。
    """
    now = time.time()
    _hist((now - 50, 'WARP-CF-1', '2a09::old'),
          (now - 10, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_last_rotate_ts", now - 0.1)   # 落在 800ms 内
    r = d._rotate_once('rate429', request_started_at=now - 50)
    assert r['reason'] == 'coalesced', r
    assert marked == [('2a09::old', True, 'rate429')], \
        f'合并窗内也必须记到发出时那条线上，实际 {marked}'


def test_pool_egress_still_backtracks(monkeypatch, marked):
    """停在轮换池（current_tag 是 None）时，回溯不能整个跳过。

    前置若写成 `current_tag and ...`，池上回来的 429 会绕过全部判定，把一条
    根本没承载过它的线记成坏地址 —— 池切换到隧道后的第一波错误正是这种形态。
    """
    now = time.time()
    monkeypatch.setattr(d, "active_warp_tag", lambda: None)
    _hist((now - 50, 'WARP-CF-1', '2a09::old'),
          (now - 10, None, None))
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=now - 50)
    assert marked == [('2a09::old', True, 'rate429')], marked


def test_sent_on_pool_is_not_blamed_on_a_tunnel(monkeypatch, marked):
    """发出时就在轮换池：那条 429 不归任何隧道，谁都不许标。"""
    now = time.time()
    monkeypatch.setattr(d, "active_warp_tag", lambda: None)
    _hist((now - 50, 'WARP-CF-1', '2a09::old'),
          (now - 10, None, None))
    monkeypatch.setattr(d, "_last_rotate_ts", 0)
    d._rotate_once('rate429', request_started_at=now - 5)
    assert marked == [], marked


def test_line_at_tolerates_out_of_order_history(monkeypatch):
    """历史里时刻倒退时，二分必须仍给出正确的那条线。

    写入点记的是「这条路什么时候**知道**地址变了」，不是「什么时候生效」：
    `_restart_warp_inplace` 在 1.4~8.7s 的模型实测**之前**取 now，实测期间
    主循环可能又换了一次线，于是列表出现 [(t+3, C), (t, B)]。

    二分不要求单调是错的 —— 对非单调序列它不是「差一格」，而是返回另一条线：
    实测 ts=t+4 真实出口是 C，未修时返回 B（WARP-CF-B），把 429 记到一条
    当时根本没在服务的线上。
    """
    _hist((103.0, 'WARP-CF-C', 'ipC'), (100.0, 'WARP-CF-B', 'ipB'))
    assert d._warp_line_at(104.0) == ('WARP-CF-C', 'ipC'), \
        f"非单调历史下给出了错误的线：{d._warp_line_at(104.0)}"
    # 更早的时刻仍要看到 B：排序不能改变区间归属。
    assert d._warp_line_at(101.0) == ('WARP-CF-B', 'ipB')


def test_line_at_keeps_latest_on_equal_timestamps():
    """同一时刻两条记录：稳定排序保留追加先后，后者胜。"""
    _hist((100.0, 'L-A', 'ipA'), (100.0, 'L-B', 'ipB'))
    assert d._warp_line_at(100.0) == ('L-B', 'ipB')


def test_inline_address_drift_is_recorded_for_active_line(monkeypatch):
    """线内换址也要落历史 —— 地址漂移不只有换线一种。

    实测 36 条线里 17 条 `ip_ts` 晚于 `sel`。只记换线的话，这期间发出的 429
    回溯拿到的是这条线上一次的地址，把早已不承载流量的地址记成新坏账。
    """
    t0 = time.time() - 100
    _hist((t0, 'WARP-CF-9', 'A-before-drift'))
    monkeypatch.setattr(d, "active_warp_tag", lambda: 'WARP-CF-9')
    d._WARP_LINE_STATE['WARP-CF-9'] = {'ip': 'A-before-drift', 'ip_ts': t0}

    d._set_warp_line_ip('WARP-CF-9', 'B-after-drift')

    assert d._warp_line_at(t0 + 30) == ('WARP-CF-9', 'A-before-drift'), \
        "漂移之前的时刻仍应指旧地址"
    assert d._warp_line_at(time.time()) == ('WARP-CF-9', 'B-after-drift'), \
        "漂移之后应指新地址"


def test_inline_drift_of_standby_line_is_not_recorded(monkeypatch):
    """非当前出口的线换址不写历史。

    全池都记的话，20 条线每分钟一轮探测就能把 128 条历史灌满，真正的换线
    记录被挤掉 —— 而迟到 429 只可能来自正在承载流量的那条。
    """
    _hist((time.time(), 'WARP-CF-9', 'A'))
    monkeypatch.setattr(d, "active_warp_tag", lambda: 'WARP-CF-9')
    d._WARP_LINE_STATE['WARP-CF-1'] = {'ip': 'X'}

    d._set_warp_line_ip('WARP-CF-1', 'Y')

    assert len(d._WARP_SWITCH_HISTORY) == 1, d._WARP_SWITCH_HISTORY


def test_reclaim_foreign_pool_records_pool_interval(monkeypatch):
    """外部翻转 selector 到池上时，历史必须补一条「那一刻在池上」。

    落池已禁用时正常路径走 `_restart_warp_inplace`，不写落池记录；而测试
    进程、手工 curl 能直连 Clash API 静默翻转（实测发生过 4 次）。不补的话，
    落池期间发出的 429 回溯停在池之前最后一条隧道上，由它替池背锅。
    """
    _hist((time.time() - 100, 'WARP-CF-1', 'old-ip'))
    monkeypatch.setattr(d, "_current_mode", lambda: ("proxy", None))
    monkeypatch.setattr(d, "_switch_warp", lambda tag, **kw: True)
    monkeypatch.setattr(d, "_LAST_POOL_RECLAIM_TS", 0.0)
    monkeypatch.setitem(d.CFG["warp"], "proxy_fallback_enabled", False)

    assert d._reclaim_foreign_pool(time.time()) is True
    assert d._WARP_SWITCH_HISTORY[-1][1] is None, d._WARP_SWITCH_HISTORY


def test_reclaim_does_not_pass_current_egress_as_target_address(monkeypatch):
    """回收时不能把「切换前那个地址」当成目标线的新地址写进历史。

    `_ACTIVE_WARP_IP` 是被换下那条 WARP 线的地址，而 target 是另一条线。
    传它等于在 target 名下记一个不属于它的地址。
    """
    _hist()
    seen = {}

    def _fake_switch(tag, **kw):
        seen["tag"] = tag
        seen["ip"] = kw.get("ip", "MISSING")
        return True

    monkeypatch.setattr(d, "_current_mode", lambda: ("proxy", None))
    monkeypatch.setattr(d, "_switch_warp", _fake_switch)
    monkeypatch.setattr(d, "_LAST_POOL_RECLAIM_TS", 0.0)
    monkeypatch.setattr(d, "_ACTIVE_WARP_IP", 'ip-of-the-line-we-came-from')
    monkeypatch.setitem(d.CFG["warp"], "proxy_fallback_enabled", False)

    d._reclaim_foreign_pool(time.time())
    assert seen["ip"] != 'ip-of-the-line-we-came-from', seen


def test_http_layer_forwards_request_started_at(monkeypatch, marked):
    _hist((100.0, 'WARP-CF-1', '2a09::old'), (200.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_reconnect_warp_by_table", _fake_switch)
    monkeypatch.setattr(d, "_last_rotate_ts", 0)

    status, payload = _post_rotate(
        monkeypatch, {"reason": "rate429", "request_started_at": 150.0})

    assert status == 200 and payload["ok"] and payload["switched"], payload
    # 发出时是 CF-1（150s），不是当前线 CF-9 —— key 没被读出来就会记成
    # CF-9 的地址。
    assert marked == [('2a09::old', True, 'rate429')], marked


def test_http_layer_garbage_timestamp_does_not_500(monkeypatch, marked):
    """时间戳给了垃圾值：整条换线请求不能因此被丢弃。"""
    _hist((100.0, 'WARP-CF-9', '2a09::new'))
    _set_line(monkeypatch, 'WARP-CF-9', lambda tag: '2a09::new')
    monkeypatch.setattr(d, "_reconnect_warp_by_table", _fake_switch)
    monkeypatch.setattr(d, "_last_rotate_ts", 0)

    status, payload = _post_rotate(
        monkeypatch, {"reason": "rate429", "request_started_at": "nonsense"})

    assert status == 200, payload
    assert marked == [('2a09::new', True, 'rate429')], marked


# ---- 时间戳解析 ----

def test_as_ts_rejects_oversized_integer():
    """`float(10**400)` 抛 OverflowError，不是 ValueError。

    不接住会穿出 /rotate 的解析段，整条换线请求变成 500 —— 客户端当成
    「没换线」，于是不重试。
    """
    assert d._as_ts(10 ** 400) is None
    assert d._as_ts(float('nan')) is None
    assert d._as_ts('abc') is None
    assert d._as_ts(123.5) == 123.5
