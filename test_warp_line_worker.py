"""warp_line_worker 单测：状态机分支 + dry-run 只读 + start/interval 行为。"""
import threading

import pytest

import warp_line_worker as w

M = {"tag": "T", "port": 1080, "service": "s"}


class Fake:
    """最小 daemon 依赖面，记录所有会产生副作用的调用。"""

    def __init__(self, ip, acc, active=None, needs=False, busy=False,
                 usable=True, reason="测试原因", members=None, line_ip=None):
        self.ip = ip
        self.line_ip = line_ip
        self.acc = acc
        self.active = active
        self.needs = needs
        self.busy = busy
        self.usable = usable
        self.reason = reason
        self.members = [] if members is None else members
        self.calls = []
        self._WARP_LAST_ACCEPT = {}

    def _warp(self):
        return {"members": self.members, "ready_timeout_sec": 12}

    def log(self, msg):
        self.calls.append(("log", msg))

    def _warp_last_accept(self, tag):
        return self.acc

    def _warp_line_ip(self, tag):
        return self.line_ip

    def _warp_model_usable(self, member, ip):
        self.calls.append(("verify", ip))
        return self.usable

    def _warp_ip_needs_refresh(self, ip, now, tag):
        return self.needs

    def _warp_ip_refresh_reason(self, ip, now, tag):
        return self.reason

    def active_warp_tag(self):
        return self.active

    def warp_line_busy(self, tag):
        return self.busy

    def _acquire_refresh_slot(self, tag):
        self.calls.append(("acquire", tag))
        return True

    def _release_refresh_slot(self, tag):
        self.calls.append(("release", tag))

    def _invalidate_warp_line_ip(self, tag):
        self.calls.append(("invalidate", tag))

    def _restart_service(self, service, tag=None):
        self.calls.append(("restart", tag))
        return True

    def _wait_port_ready(self, port, timeout_sec):
        return True

    def _probe_refresh_egress(self, port):
        return self.ip

    def _mark_warp_ip_used(self, ip, now, blocked=False, **kw):
        self.calls.append(("used", ip, blocked))

    def _reconnect_warp_now(self, reason, now, ignore_cooldown=False):
        self.calls.append(("switch", reason))
        return True

    def _set_warp_line_ip(self, tag, ip):
        self.calls.append(("set_ip", tag, ip))

    def _warp_refresh_progress(self, tag):
        return ""


@pytest.fixture(autouse=True)
def _reset_globals():
    w._threads.clear()
    w._LAST_LOG.clear()
    w._host = None
    w._DRY_RUN = False
    w._INTERVAL = w.INTERVAL_SEC
    yield
    w._threads.clear()
    w._LAST_LOG.clear()
    w._host = None
    w._DRY_RUN = False
    w._INTERVAL = w.INTERVAL_SEC


def run(fake, dry_run=False):
    w._host = fake
    w._LAST_LOG.clear()
    fake.calls.clear()
    w.tick(M, dry_run=dry_run)
    return fake.calls


def acted(calls, kind):
    return any(c[0] == kind for c in calls)


def logs(calls):
    return " ".join(c[1] for c in calls if c[0] == "log")


def test_healthy_line_no_action_and_observation_log():
    c = run(Fake("ip1", (1.0, "ip1")))
    assert not acted(c, "restart") and not acted(c, "switch")
    assert not any(x[0] == "set_ip" for x in c)
    assert "观测 出口=ip1" in logs(c)


def test_address_change_marks_old_and_accepts_new():
    c = run(Fake("ip2", (1.0, "ip1")))
    assert ("used", "ip1", False) in c
    assert ("verify", "ip2") in c
    assert ("set_ip", "T", "ip2") in c
    assert not acted(c, "restart")


def test_probe_missing_is_not_restart():
    c = run(Fake(None, None))
    assert not acted(c, "restart") and not acted(c, "switch")
    assert "出口探测未果" in logs(c)


def test_same_address_low_quality_restarts():
    c = run(Fake("ip1", (1.0, "ip1"), needs=True))
    assert ("restart", "T") in c


def test_active_same_address_never_restarts():
    c = run(Fake("ip1", (1.0, "ip1"), active="T", needs=True))
    assert not acted(c, "restart") and not acted(c, "switch")


def test_busy_line_waits_without_restart():
    c = run(Fake("ip1", (1.0, "ip1"), needs=True, busy=True))
    assert not acted(c, "restart")
    assert not acted(c, "acquire")
    assert "线上有连接" in logs(c)


def test_unverified_new_address_verification_failure_restarts():
    c = run(Fake("ip9", None, usable=False))
    assert ("verify", "ip9") in c
    assert ("restart", "T") in c


def test_active_address_change_switches_without_restart_or_verify():
    c = run(Fake("ip9", (1.0, "ip1"), active="T", usable=False))
    assert not acted(c, "restart")
    assert acted(c, "switch")
    assert not any(x[0] == "verify" for x in c)
    assert ("used", "ip1", False) in c
    assert not any(x[0] == "set_ip" for x in c)


def test_low_quality_restart_reason_carries_ip():
    c = run(Fake("ip7", (1.0, "ip7"), needs=True))
    joined = logs(c)
    assert "ip7" in joined
    assert "出口未知" not in joined


def test_dry_run_active_switch_logs_only():
    c = run(Fake("ip2", (1.0, "ip1"), active="T"), dry_run=True)
    assert not acted(c, "switch")
    assert not any(x[0] == "used" for x in c)
    assert not any(x[0] == "set_ip" for x in c)
    assert "[dry-run]" in logs(c) and "应换线" in logs(c)


def test_dry_run_unverified_does_not_burn_model_quota():
    """dry-run 不得调 _warp_model_usable：它会真实扣配额并写探测会话。"""
    c = run(Fake("ip9", None, usable=False), dry_run=True)
    assert not acted(c, "verify")
    assert not acted(c, "restart")
    assert not acted(c, "acquire")
    assert not acted(c, "invalidate")
    assert "[dry-run]" in logs(c) and "应模型验收" in logs(c)


def test_dry_run_accept_logs_without_ledger():
    fake = Fake("ip9", None, usable=True)
    c = run(fake, dry_run=True)
    assert not acted(c, "verify")
    assert not any(x[0] == "set_ip" for x in c)
    assert not any(x[0] == "used" for x in c)
    assert fake._WARP_LAST_ACCEPT == {}
    assert "[dry-run]" in logs(c) and "应模型验收" in logs(c)


def test_stale_observation_does_not_restart_new_active_line():
    """决策后这条线被选为当前出口：必须放弃 restart，不能在途请求被掐断。"""
    class FlipActive(Fake):
        def __init__(self):
            super().__init__("ip9", None, active=None, usable=False)
            self.checks = 0

        def active_warp_tag(self):
            self.checks += 1
            return None if self.checks == 1 else "T"

    fake = FlipActive()
    c = run(fake)
    assert acted(c, "verify")           # 决策阶段确实验过
    assert not acted(c, "restart")      # 但执行前发现已变当前出口
    assert not acted(c, "acquire")
    assert "取消 restart" in logs(c)


def test_stale_observation_does_not_switch_away_new_active_line():
    """决策后这条线已不是当前出口：不能再触发换线，把新出口切走。"""
    class FlipActive(Fake):
        def __init__(self):
            super().__init__("ip9", (1.0, "ip1"), active="T")
            self.checks = 0

        def active_warp_tag(self):
            self.checks += 1
            return "T" if self.checks == 1 else "OTHER"

    c = run(FlipActive())
    assert not acted(c, "switch")
    assert "取消换线" in logs(c)


def test_restart_bootstrap_uses_persisted_line_ip_as_verified():
    """daemon 重启后内存验收为空：持久化线路 IP 相同就不该换走当前出口。"""
    c = run(Fake("ip1", None, active="T", line_ip="ip1"))
    assert not acted(c, "verify")
    assert not acted(c, "switch")
    assert not acted(c, "restart")


def test_standby_bootstrap_does_not_burn_model_quota():
    c = run(Fake("ip1", None, active="OTHER", line_ip="ip1"))
    assert not acted(c, "verify")
    assert not acted(c, "restart")
    assert not acted(c, "set_ip")


def test_active_rechecked_after_slot_acquisition():
    """判闲/拿槽期间变成当前出口，重启前必须再次发现并放弃。"""
    class ThirdCheckActive(Fake):
        def __init__(self):
            super().__init__("ip9", None, active=None, usable=False)
            self.checks = 0

        def active_warp_tag(self):
            self.checks += 1
            return "T" if self.checks >= 3 else None

    c = run(ThirdCheckActive())
    assert not acted(c, "restart")
    assert "已成为当前出口，取消重启" in logs(c)


def test_accept_skipped_when_ip_changed_during_model_probe():
    class Shifted(Fake):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.probes = 0

        def _probe_refresh_egress(self, port):
            self.probes += 1
            return self.ip if self.probes == 1 else "ip-other"

    fake = Shifted("ip9", None, active="OTHER")
    w._host = fake
    w._LAST_LOG.clear()
    fake.calls.clear()
    w.tick(M)
    assert not any(x[0] == "set_ip" for x in fake.calls)
    assert fake._WARP_LAST_ACCEPT == {}
    assert "模型验收期间出口变更" in logs(fake.calls)


@pytest.mark.parametrize("bad", [0, -0.1])
def test_start_rejects_non_positive_interval(bad):
    with pytest.raises(ValueError, match="interval_sec"):
        w.start(Fake("ip1", ("ip1",)), members=[M], interval_sec=bad)


@pytest.mark.parametrize("active,verified,ip,probe,usable,needs", [
    (True, True, "ip1", True, True, False),
    (True, False, "ip1", True, True, False),
    (False, True, "ip1", True, True, False),
    (False, False, "ip1", False, True, False),
    (False, False, "ip1", True, True, False),
    (False, False, "ip1", True, False, False),
])
def test_plan_never_returns_removed_none_action(
        active, verified, ip, probe, usable, needs):
    """_plan 的契约只有 None/restart/switch/accept/probe，不再有 none。"""
    fake = Fake(ip, ("ip1",), active="T" if active else "OTHER",
                usable=usable, needs=needs)
    w._host = fake
    plan = w._plan(M, ip, fake.acc, active, verified, 1000.0, probe=probe)
    assert plan is None or plan[0] in {"restart", "switch", "accept", "probe"}


def test_start_empty_members_no_threads():
    fake = Fake(None, None)
    w.start(fake, members=[])
    assert w._threads == {}
    w.start(fake)
    assert w._threads == {}
    assert w._DRY_RUN is False
    assert w._INTERVAL == w.INTERVAL_SEC


def test_start_twice_keeps_single_thread(monkeypatch):
    started = threading.Event()
    stop = threading.Event()

    def fake_loop(member, dry_run, interval_sec):
        started.set()
        stop.wait(5)

    monkeypatch.setattr(w, "_loop", fake_loop)
    fake = Fake(None, None, members=[M])
    w.start(fake, members=[M], dry_run=True, interval_sec=1.5)
    assert started.wait(2)
    first = w._threads["T"]
    assert first.is_alive()

    w.start(fake, members=[M], dry_run=False, interval_sec=2.5)
    assert w._threads["T"] is first
    assert len(w._threads) == 1
    assert w._DRY_RUN is False
    assert w._INTERVAL == 2.5

    stop.set()
    first.join(2)
    assert not first.is_alive()


class StopLoop(Exception):
    pass


@pytest.mark.parametrize("remaining", [[], [{"tag": "NEW", "port": 1099,
                                               "service": "n.service"}]],
                         ids=["last-member-removed", "others-remain"])
def test_loop_stops_when_member_removed_from_config(monkeypatch, remaining):
    """配置移除后线程必须退出：否则会继续重启一个已删除的 unit。

    空表是「全部移除」，同样要退；只有读取失败(None)才保持运行。
    """
    fake = Fake(None, None, members=remaining)
    w._host = fake
    w._threads["T"] = object()
    w._LAST_ERR["T"] = (1.0, "old")
    ticked = []
    monkeypatch.setattr(w, "tick", lambda m, **k: ticked.append(m["tag"]))
    # 正常路径在 sleep 前 return；变异（去掉移除检查）会落到 sleep 抛错，
    # 用例立刻变红而不是挂 12.5s。
    monkeypatch.setattr(w.time, "sleep", lambda s: (_ for _ in ()).throw(StopLoop))
    w._loop(M, False, 12.5)
    assert ticked == [], "移除后不得再 tick"
    assert "T" not in w._threads and "T" not in w._LAST_ERR
    assert any("已从配置移除" in msg for kind, msg in fake.calls)


def test_loop_throttles_repeated_error_logs(monkeypatch):
    """同一异常每 5s 一条会刷满日志；60s 内只记一次。"""
    fake = Fake(None, None, members=[M])
    w._host = fake
    w._LAST_ERR.clear()
    monkeypatch.setattr(w, "tick", lambda m, **k: (_ for _ in ()).throw(
        RuntimeError("boom")))
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) >= 3:
            raise StopLoop

    monkeypatch.setattr(w.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        w._loop(M, False, 5.0)
    errs = [msg for kind, msg in fake.calls if "线程异常" in msg]
    assert len(errs) == 1, f"3 轮只应记 1 条，实际 {errs}"


def test_loop_uses_interval_sec(monkeypatch):
    fake = Fake(None, None, members=[M])
    w._host = fake
    seen = []

    def fake_sleep(sec):
        seen.append(sec)
        raise StopLoop

    monkeypatch.setattr(w.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        w._loop(M, False, 12.5)
    assert seen == [12.5]
