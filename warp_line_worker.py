"""每条 WARP 隧道一个常驻 worker：自己判质量、自己重启、自己验收。

与旧的三处守护（预热 `_prewarm_standby_warp` / 脏线抽检 `_dirty_line_janitor`
/ 补刷 `_retry_refresh_idle_warp`）的区别只有一条：**判断和执行在同一个循环里**。
旧实现把「谁该刷」和「怎么刷」拆成两个调度器，抽检选中的线还要排队等名额、
等空闲、等冷却，一轮下来 IP 早换了三轮。

这里的口径直接对应用户的三句话：
  - 不排队、不预睡：一个 tick 一次判断，5s 只是防打爆，不是调度周期。
  - 判断对象是**所有**线：地址变了就把旧地址入库、新地址重新验收；
    地址没变就只读台账，不重复烧配额。
  - 低质动作固定为「重启 → 探出口 → 入池 → 模型验收」，
    验收不过不等待，下一个 tick 继续重启，直到 OK。

通过 `start(host)` 注入 daemon 模块对象，模块自己不 import daemon ——
daemon 既可能被 import（测试）也可能作为 __main__ 运行，直接 import 会二次加载。

`start(..., dry_run=True)` 时只探出口并记录本应执行的动作，不重启、不换线、
不写台账，用于接入 daemon 前观察行为。
"""
import threading
import time
from typing import Protocol


class DaemonHost(Protocol):
    """本模块对 daemon 的最小依赖面。

    worker 通过 `start(host)` 注入 daemon 模块对象，模块自身不 import daemon
    （daemon 可能以 __main__ 运行）。把这份契约写成 Protocol 而不是散落在
    各处的 `h.xxx`，改 daemon 接口时能一眼看出会影响谁。
    """

    _WARP_LAST_ACCEPT: dict[str, tuple[float, str]]

    def log(self, msg: str) -> None: ...
    def _warp(self) -> dict: ...
    def active_warp_tag(self) -> str | None: ...
    def _warp_last_accept(self, tag: str) -> tuple[float, str] | None: ...
    def _probe_refresh_egress(self, port: int) -> str | None: ...
    def _mark_warp_ip_used(self, ip: str | None, now: float,
                           blocked: bool = False, kind: str | None = None) -> None: ...
    def _warp_refresh_progress(self, tag: str) -> str: ...
    def _warp_model_usable(self, member: dict, ip: str) -> bool: ...
    def _warp_ip_needs_refresh(self, ip: str, now: float, tag: str) -> bool: ...
    def _warp_ip_refresh_reason(self, ip: str, now: float, tag: str) -> str | None: ...
    def _reconnect_warp_now(self, reason: str, now: float,
                            ignore_cooldown: bool = False) -> bool: ...
    def warp_line_busy(self, tag: str) -> bool | None: ...
    def _acquire_refresh_slot(self, tag: str) -> bool: ...
    def _release_refresh_slot(self, tag: str) -> None: ...
    def _invalidate_warp_line_ip(self, tag: str) -> None: ...
    def _restart_service(self, service: str, tag: str | None = None) -> bool: ...
    def _wait_port_ready(self, port: int, timeout_sec: float) -> bool: ...
    def _set_warp_line_ip(self, tag: str, ip: str) -> None: ...


_host: DaemonHost | None = None
_threads: dict[str, threading.Thread] = {}
# 每条线上一次观测日志的状态与时刻。20 条线 × 5s 一轮 = 每分钟 240 行，
# 全是「出口没变、验过、健康」的同样内容 —— 真出事的那一行会被埋掉。
# 只在状态变了或满心跳间隔时打一行。
_LAST_LOG: dict[str, tuple[str, float]] = {}
HEARTBEAT_SEC = 60.0

# 一轮的间隔。出口 IP 存活中位 12.5s，5s 一轮保证一条脏线在一个 IP 寿命内
# 至少被看到两次；再快只是把 curl 打成风暴。
INTERVAL_SEC = 5.0
# 出口探测不复用单次预算：tick 走 daemon 的 _probe_refresh_egress（轮询到通为止）。

# start() 保存的当前配置。线程创建时把值一并传进 _loop，已存活线程不受
# 后续 start() 调用的影响。
_DRY_RUN = False
_INTERVAL = INTERVAL_SEC

_DRY_RUN_LABEL = {
    "switch": "应换线",
    "restart": "应重启",
    "accept": "应验收",
    "none": "无动作",
}


def start(host: DaemonHost, members: list[dict] | None = None, *,
          dry_run: bool = False, interval_sec: float | None = None) -> None:
    """为每条隧道起一个常驻 worker。重复调用无副作用。"""
    global _host, _DRY_RUN, _INTERVAL
    _host = host
    _DRY_RUN = bool(dry_run)
    _INTERVAL = INTERVAL_SEC if interval_sec is None else float(interval_sec)

    if members is None:
        members = host._warp()["members"]
    if not members:
        return

    for m in members:
        tag = m["tag"]
        t = _threads.get(tag)
        if t is not None and t.is_alive():
            continue
        t = threading.Thread(target=_loop, args=(m, _DRY_RUN, _INTERVAL),
                             daemon=True, name=f"warp-line-{tag}")
        _threads[tag] = t
        t.start()


def _loop(member: dict, dry_run: bool = False,
          interval_sec: float | None = None) -> None:
    interval = INTERVAL_SEC if interval_sec is None else interval_sec
    while True:
        try:
            tick(member, dry_run=dry_run)
        except Exception as e:
            _host.log(f"  ✗ {member['tag']} 线程异常: {e}")
        time.sleep(interval)


def tick(member: dict, *, dry_run: bool = False) -> None:
    """一轮判断 + 一轮动作。所有线都走这里，不只是低质线。

    判据分两种，不能混：「探不到出口」是观测缺失，「探到了但判低质」才是结论。
    只有后者构成重启理由 —— 探不到就重启，等于拿重启去解一个探测问题，
    新会话照样可能在冷启动窗口里（见 daemon._probe_refresh_egress）。
    """
    h = _host
    tag, port = member["tag"], member["port"]
    # 用轮询探测而不是单次 7s：隧道刚重连时出口可用时间实测 5.0~8.3s，
    # 单次预算正压在分布中位数上，探空一次就被判「出口未知」→ 重启 →
    # 新会话又落在冷启动段。这条路径上有过 1 小时 763 次 systemd Started
    # 的前科（见 daemon._probe_refresh_egress 的 docstring）。
    # 代价：真坏的线每轮最多占住自己这个线程 36s，不影响其他线。
    t0 = time.time()
    ip = h._probe_refresh_egress(port)
    now = time.time()
    acc = h._warp_last_accept(tag)
    verified = bool(ip and acc and acc[1] == ip)
    active = tag == h.active_warp_tag()

    if not ip:
        # 观测缺失不是结论：不重启，下一轮继续探。打一行便于区分「探不到」
        # 与「探到了但低质」—— 两者的处置完全相反，日志必须一眼可辨。
        _note(tag, "noip",
              f"  ? {tag} 出口探测未果（{now - t0:.1f}s），本轮不动作，下一轮继续探"
              f"{_ctx(tag, acc, active, verified, None)}")
        return

    _note(tag, f"obs:{ip}:{verified}",
          f"  · {tag} 观测 出口={ip} 验收={'同址已验过' if verified else '新址待验'}"
          f"{_ctx(tag, acc, active, verified, ip)}")

    plan = _plan(member, ip, acc, active, verified, now)
    if plan is None or plan[0] == "none":
        return
    action, reason = plan

    # dry-run 只读：出口探测能看出「本该做什么」，但绝不写台账、不重启、不换线。
    if dry_run:
        _note(tag, f"dry:{action}:{ip}",
              f"  [dry-run] {tag} {_DRY_RUN_LABEL.get(action, action)}"
              f"（本应执行 {action}）原因={reason}"
              f"{h._warp_refresh_progress(tag)}")
        return

    # 地址变了：变化前那个地址确实服务过，入台账（换出来的 IP 该入库就入库）。
    if acc and not verified:
        h._mark_warp_ip_used(acc[1], now)
        h.log(f"  ⇄ {tag} 出口变更 {acc[1]} → {ip}，旧址入地址台账")

    if action == "switch":
        _note(tag, f"swap:{ip}",
              f"  ⇄ {tag} 当前出口换址 {acc[1] if acc else '未知'} → {ip}，立即换线"
              f"{h._warp_refresh_progress(tag)}")
        _switch_away(member, reason)
    elif action == "accept":
        _accept(member, ip)
    elif action == "restart":
        _restart_and_verify(member, reason)


def _plan(member: dict, ip: str | None, acc: tuple | None, active: bool,
          verified: bool, now: float) -> tuple[str, str] | None:
    """把观测映射成一个待执行动作，本身不做任何写操作。

    action 取 restart/switch/accept/none，无需动作时返回 None。执行交给 tick，
    这样 dry_run 只需跳过执行即可保证零副作用。
    """
    h = _host
    tag = member["tag"]
    if not ip:
        return None

    # 当前出口：重启它等于掐断在途请求，所以地址没变就什么都不做。地址变了
    # 必须换线：新地址的配额/信誉全新且未知，继续服务生产流量等于拿真实请求
    # 当探针（首条错误延迟 p50 只有 1.6s）。
    if active:
        if verified:
            return None
        return ("switch", f"当前出口换址 {ip}")

    # 未验收过的新地址：先验，验过就入池，不过就重启。
    if not verified:
        if h._warp_model_usable(member, ip):
            return ("accept", f"新址 {ip} 验收通过")
        return ("restart", f"新址 {ip} 验收未过")

    # 已验收且台账健康：什么都不做。只读台账，不烧配额。
    if not h._warp_ip_needs_refresh(ip, now, tag):
        return None
    return ("restart", h._warp_ip_refresh_reason(ip, now, tag) or "台账判低质")


def _note(tag: str, key: str, line: str) -> None:
    """同一状态只在变化时打一行，长期不变每 HEARTBEAT_SEC 补一行。

    WHY 不做全量打印：20 条线 5s 一轮就是每分钟 240 行同样的「出口没变、
    已验过、健康」。日志 5MB 轮转，全量打印 2.3 小时就冲掉一遍历史，
    真出事的那几行被埋在里面。状态变化与心跳并存才既能看到即时变化，
    又能在事后确认「这段时间一直是这样」。
    """
    prev = _LAST_LOG.get(tag)
    now = time.time()
    if prev and prev[0] == key and now - prev[1] < HEARTBEAT_SEC:
        return
    _LAST_LOG[tag] = (key, now)
    _host.log(line + ("（状态未变，心跳）"
                      if prev and prev[0] == key else ""))


def _ctx(tag: str, acc: tuple | None, active: bool, verified: bool,
         ip: str | None) -> str:
    """观测上下文后缀：这条线在哪、上次验收的是哪个地址、多久以前。

    WHY 每次判定都带：排查时「为什么这条线不动」最常见的三种原因是
    「它是当前出口」「它刚验过同一地址」「它验的是另一个地址」——
    只看一行 `✓ 模型层 OK` 无法区分，得回头翻状态。
    """
    h = _host
    bits = [f"当前出口={'是' if active else '否'}"]
    if acc:
        bits.append(f"上次验收={acc[1]}（{(time.time() - acc[0]) / 60:.1f}min 前）")
    else:
        bits.append("上次验收=无")
    if ip and acc:
        bits.append("地址一致" if verified else "地址已变")
    return "，" + "，".join(bits) + f"{h._warp_refresh_progress(tag)}"


def _accept(member: dict, ip: str) -> None:
    h = _host
    tag = member["tag"]
    h._set_warp_line_ip(tag, ip)
    h._WARP_LAST_ACCEPT[tag] = (time.time(), ip)
    h._mark_warp_ip_used(ip, time.time())


def _switch_away(member: dict, reason: str) -> None:
    """当前出口不合格时换个出口：不自重启，走 daemon 的换线路径。

    WHY 不用 `_restart_and_verify`：当前出口上有在途请求，重启它会让 9router
    报 upstream connection lost。换线路径是把 selector 切到另一条已验证的线
    （PUT，秒级，在途连接不断），再把这条脏线排队刷新 —— 分工是对的，别在这里
    抢。

    WHY ignore_cooldown=True：当前出口换址必须「马上切换」（用户原话），不等验收。
    冷却是防频繁切线抖动，但出口 IP 已变 = 新地址额度/信誉全未知，继续服务生产
    流量等于拿真实请求当探针。这里绕过冷却是正确的：_WARP_LOCK 仍防并发，
    但不因 5s 冷却延误首换线。一次换址风暴由 tick 5s 节流自然吸收。
    """
    h = _host
    tag = member["tag"]
    now = time.time()
    if h._reconnect_warp_now(reason, now, ignore_cooldown=True):
        h.log(f"  ⇄ {tag} 触发换线（{reason}）")
    else:
        # 并发锁挡住：不是失败。下一轮 tick 会再判一次，那时地址可能
        # 又变了（usque 换址频繁），重试的语义天然由 tick 承担。
        h.log(f"  ⏸ {tag} 需换线但被并发占用（{reason}），下一轮再试")


def _restart_and_verify(member: dict, reason: str) -> None:
    """重启 → 等端口 → 探出口 → 入池 → 模型验收。失败就返回，下一 tick 再来。"""
    h = _host
    tag, port = member["tag"], member["port"]

    # 有连接在传数据就不重启：只等，不抢名额。下一个 tick 再查一次。
    busy = h.warp_line_busy(tag)
    if busy is not False:
        h.log(f"  ⏸ {tag} 待重启但线上有连接（busy={busy}），本轮不重启 原因={reason}"
              f"{h._warp_refresh_progress(tag)}")
        return
    if not h._acquire_refresh_slot(tag):
        h.log(f"  ⏸ {tag} 待重启但刷新槽已被占用，本轮跳过 原因={reason}")
        return
    h.log(f"  ↻ {tag} 重启中 原因={reason}{h._warp_refresh_progress(tag)}")
    try:
        h._invalidate_warp_line_ip(tag)
        ok = (h._restart_service(member["service"], tag)
              and h._wait_port_ready(port, h._warp()["ready_timeout_sec"]))
        if not ok:
            h.log(f"  ✗ {tag} 重启后端口未就绪 原因={reason}"
                  f"{h._warp_refresh_progress(tag)}")
            return

        new_ip = h._probe_refresh_egress(port)
        if not new_ip:
            # 端口起来了但探不到出口：不是「坏节点」，是这条线还没真正通。
            # 不追加重启（重启只会再进一次冷启动），交给下一轮继续探。
            h.log(f"  ✗ {tag} 重启后端口中继探不到出口 原因={reason}"
                  f"{h._warp_refresh_progress(tag)}")
            return

        h._mark_warp_ip_used(new_ip, time.time())   # 换出来的地址入库
        if h._warp_model_usable(member, new_ip):
            _accept(member, new_ip)
            h.log(f"  ✓ {tag} 刷新成功 原因={reason} 新出口 {new_ip}"
                  f"{h._warp_refresh_progress(tag)}")
        else:
            h.log(f"  ✗ {tag} 新出口 {new_ip} 验收未过，下一轮继续重启"
                  f"{h._warp_refresh_progress(tag)}")
    finally:
        h._release_refresh_slot(tag)


# ---- 自检：不需要隧道，验证状态机分支不回归 ----
if __name__ == "__main__":
    calls = []

    class Fake:
        def __init__(self, ip, acc, active=None, needs=False, busy=False,
                     usable=True):
            self.ip, self.acc, self.active = ip, acc, active
            self.needs, self.busy, self.usable = needs, busy, usable

        def _warp(self):
            return {"members": [], "ready_timeout_sec": 12}

        def log(self, m):
            calls.append(("log", m))

        def warp_egress_ip(self, port, timeout_sec=20):
            return self.ip

        def _warp_last_accept(self, tag):
            return self.acc

        def _warp_model_usable(self, member, ip):
            calls.append(("verify", ip))
            return self.usable

        def _warp_ip_needs_refresh(self, ip, now, tag):
            return self.needs

        def _warp_ip_refresh_reason(self, ip, now, tag):
            return "测试原因"

        def active_warp_tag(self):
            return self.active

        def warp_line_busy(self, tag):
            return self.busy

        def _acquire_refresh_slot(self, tag):
            return True

        def _release_refresh_slot(self, tag):
            pass

        def _invalidate_warp_line_ip(self, tag):
            pass

        def _restart_service(self, service, tag=None):
            calls.append(("restart", tag))
            return True

        def _wait_port_ready(self, port, t):
            return True

        def _probe_refresh_egress(self, port):   # tick 现在走轮询探测：
            return self.ip                       # 返回 None 即「轮询到底仍无出口」

        def _mark_warp_ip_used(self, ip, now, blocked=False, **kw):
            calls.append(("used", ip, blocked))

        def _reconnect_warp_now(self, reason, now, ignore_cooldown=False):
            calls.append(("switch", reason))
            return True

        def _set_warp_line_ip(self, tag, ip):
            pass

        def _warp_refresh_progress(self, tag):
            return ""

        _WARP_LAST_ACCEPT: dict = {}

    M = {"tag": "T", "port": 1080, "service": "s"}

    def run(fake):
        global _host, calls
        _host = fake
        calls = []
        _LAST_LOG.clear()      # 每个用例独立：去重状态跨用例会吞掉日志
        tick(M)
        return list(calls)

    def acted(c, kind="restart"):
        return any(x[0] == kind for x in c)

    # 1) 地址未变 + 台账健康 → 零动作（只留一行观测日志）
    c = run(Fake("ip1", (1.0, "ip1")))
    assert not acted(c), f"健康线不该重启: {c}"
    assert any(x[0] == "log" and "观测 出口=ip1" in x[1] for x in c), c
    # 2) 地址变了 → 旧址入台账 + 新址验收
    c = run(Fake("ip2", (1.0, "ip1")))
    assert ("used", "ip1", False) in c, c
    assert ("verify", "ip2") in c, c
    # 3) 探不到出口 → 不重启，只记「本轮不动作」（观测缺失不是结论）
    c = run(Fake(None, None))
    assert not acted(c), f"探不到出口不得重启: {c}"
    assert any(x[0] == "log" and "出口探测未果" in x[1] for x in c), c
    # 4) 同址但台账判低质 → 重启（不能因为地址没变就放过）
    c = run(Fake("ip1", (1.0, "ip1"), needs=True))
    assert acted(c), c
    # 5) 当前出口 → 永不重启
    c = run(Fake("ip1", (1.0, "ip1"), active="T", needs=True))
    assert not acted(c), c
    # 6) 址在但有连接在传数据 → 只等不重启
    c = run(Fake("ip1", (1.0, "ip1"), needs=True, busy=True))
    assert not acted(c), c
    assert any(x[0] == "log" and "线上有连接" in x[1] for x in c), c
    # 7) 新址验收不过 → 重启
    c = run(Fake("ip9", None, usable=False))
    assert acted(c), c
    # 8) 当前出口换了新址 → 立刻换线；绝不能原地重启（会掐断在途请求）
    c = run(Fake("ip9", (1.0, "ip1"), active="T", usable=False))
    assert not acted(c), f"当前出口不得原地重启: {c}"
    assert acted(c, "switch"), f"当前出口换址必须立刻换线: {c}"
    # 8b) 换址后不再对新址做模型验收：额度校验交给下游备线路径，
    #     生产流量不该充当探针。
    assert not any(x[0] == "verify" for x in c), f"换址路径不得再验收: {c}"
    # 8c) 旧址必须入台账（它确实服务过流量）
    assert ("used", "ip1", False) in c, f"旧址应入台账: {c}"
    # 8d) 当前出口地址没变 → 零动作，既不换线也不重启
    c = run(Fake("ip1", (1.0, "ip1"), active="T", usable=False))
    assert not acted(c) and not acted(c, "switch"), c
    # 9) 探到出口但台账判低质 → 日志里必须出现具体 IP，不能只说「出口未知」
    c = run(Fake("ip7", (1.0, "ip7"), needs=True))
    joined = " ".join(x[1] for x in c if x[0] == "log")
    assert "ip7" in joined, f"重启原因必须带出 IP: {joined}"
    assert "出口未知" not in joined, f"有 IP 时不得写「出口未知」: {joined}"
    print("selftest OK (9 cases)")
