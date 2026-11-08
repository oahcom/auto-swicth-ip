"""设备用完后自造补位线（daemon._ensure_backfill_line）。

`_backfill_spare_indices` 只认「已注册 **且** 单元已装」的线。实测 2026-11-04：
31 条 WG 线里 27 被 mask、31 注册了没单元、其余全在 members —— 补位日志每轮
报「没有能起设备的空闲线」，7 条 H2 线整晚没有补位。缺的就是这一步。

用 AST 抽函数体，不 import daemon（会拉起线程和全局状态）。
"""
import ast
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
NAMES = ("_wg_line_state", "_last_wg_register_ts", "_ensure_backfill_line")


def _impl():
    tree = ast.parse((PROJECT / "daemon.py").read_text())
    picked = [n for n in tree.body if getattr(n, "name", None) in NAMES]
    assert len(picked) == len(NAMES), "daemon.py 里找不到补位自造函数"
    return picked


class _Env:
    """伪造设备目录、单元目录、members 与注册/装单元两个副作用。"""

    def __init__(self, tmp_path, registered, members, units, last_register=-1e9):
        self.tmp = tmp_path
        self.registered = list(registered)      # 有 profile.conf 的线号
        self.members = list(members)            # 已在 members 的 tag
        self.units = dict(units)                # 线号 -> installed|masked|missing
        self.installed: list[int] = []
        self.registered_new: list[int] = []
        self.install_ok = True
        self.now = 10_000.0
        # 最近一次注册时刻（`profile.conf` mtime）。默认很久以前 = 不受限速。
        self.last_register = last_register

        wgdir = tmp_path / "wglines"
        for i in self.registered:
            d = wgdir / f"line{i}"
            d.mkdir(parents=True)
            (d / "profile.conf").write_text("[Interface]\n")
        self.wgdir = wgdir

        unitdir = tmp_path / "systemd"
        unitdir.mkdir()
        self.unitdir = unitdir
        for i, kind in self.units.items():
            p = unitdir / f"warp-wg-{i}.service"
            if kind == "installed":
                p.write_text("[Unit]\n")
            elif kind == "masked":
                p.symlink_to("/dev/null")

    # ---- 假 os.path：把 /etc/systemd/system 重定向到 tmp ----
    def _map(self, p):
        name = os.path.basename(str(p))
        if str(p).startswith("/etc/systemd/system/"):
            return self.unitdir / name
        return Path(str(p))

    def exists(self, p):
        return self._map(p).exists() or os.path.exists(str(self._map(p)))

    def realpath(self, p):
        q = self._map(p)
        if q.is_symlink():
            return "/dev/null" if os.readlink(q) == "/dev/null" else str(q)
        return str(q)

    def install(self, i):
        """假 `_install_wg_unit`：成功就落一个单元文件。"""
        if not self.install_ok:
            return False
        (self.unitdir / f"warp-wg-{i}.service").write_text("[Unit]\n")
        self.installed.append(i)
        return True


class _Wg:
    def __init__(self, env):
        self.env = env

    def line_dir(self, i):
        return self.env.wgdir / f"line{i}"

    def line_tag(self, i):
        return f"WARP-WG-{i}"

    def unit_name(self, i):
        return f"warp-wg-{i}.service"

    def register_line(self, i, force=False):
        d = self.env.wgdir / f"line{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.conf").write_text("[Interface]\n")
        self.env.registered.append(i)
        self.env.registered_new.append(i)


def _run(env, max_new=1, gap=600.0, last_register=-1e9, max_lines=64):
    """执行函数体，返回 (结果, 新 _LAST_WG_CREATE_TS)。"""
    wg = _Wg(env)

    def spare_indices():
        """与 daemon 同判据的简化版：注册 + 未进 members + 单元已装。"""
        out = []
        for i in env.registered:
            if f"WARP-WG-{i}" in env.members:
                continue
            q = env.unitdir / f"warp-wg-{i}.service"
            real = "/dev/null" if q.is_symlink() else str(q)
            if real == "/dev/null" or not q.exists():
                continue
            out.append(i)
        return out

    box = {
        "os": type("O", (), {"path": type("P", (), {
            "exists": staticmethod(env.exists),
            "realpath": staticmethod(env.realpath)})()})(),
        "time": type("T", (), {"time": staticmethod(lambda: env.now)})(),
        "warp_wg": wg,
        "_warp": lambda: {"members": [{"tag": t} for t in env.members]},
        "_backfill_spare_indices": spare_indices,
        "_install_wg_unit": env.install,
        "log": lambda *a, **k: None,
        "_PROJECT": str(PROJECT),
        "_WG_CREATE_GAP": gap,
        "_WG_MAX_LINES": max_lines,
        # 档位停用的线不参与补位。测试里没有档位文件，按「全开」给空集合。
        "_TIER_DISABLED": set(),
    }
    exec(compile(ast.Module(body=_impl(), type_ignores=[]), "daemon.py", "exec"),
         box)
    # 必须在 exec **之后**覆盖：exec 会把真函数装进 globals，盖掉预先放的桩。
    box["_last_wg_register_ts"] = lambda: env.last_register
    return box["_ensure_backfill_line"](max_new=max_new), box


def test_registered_line_without_unit_gets_a_unit_first(tmp_path):
    """已注册但没单元的线优先 —— 装单元不烧 CF 注册额度。"""
    env = _Env(tmp_path, registered=[1, 31], members=["WARP-WG-1"],
               units={1: "installed", 31: "missing"})
    got, ts = _run(env)
    assert env.installed == [31], "应先给 31 补单元，而不是注册新设备"
    assert env.registered_new == []
    assert got == [31]


def test_masked_line_is_never_resurrected(tmp_path):
    """mask（软链 /dev/null）是主人手工停用的，不许自动装回来。

    可以另起一条新线（走注册），但绝不能把 27 重新装单元 —— 那会把它从
    「已停用」变回「可用」，等于自动撤销主人的决定。
    """
    env = _Env(tmp_path, registered=[27], members=[], units={27: "masked"})
    got, _ = _run(env)
    assert 27 not in env.installed, "masked 线路被自动恢复了"
    assert env.registered_new == [28], "没有其它储量时应注册新设备"
    assert got == [28]


def test_registers_new_device_when_no_line_exists(tmp_path):
    """一台储量都没有时注册新设备，接着最大槽位往后编。"""
    env = _Env(tmp_path, registered=[1, 2], members=["WARP-WG-1", "WARP-WG-2"],
               units={1: "installed", 2: "installed"})
    got, _ = _run(env)
    assert env.registered_new == [3]
    assert got == [3]


def test_gap_blocks_second_creation(tmp_path):
    """十分钟内只注册一条：注册一台就烧一格 CF 额度，爆封时多个渠道同时缺线，
    一轮全注册会把窗口吃光，后面几轮全被 429。

    限速读的是**最新 profile.conf 的 mtime**，不是进程内变量 —— daemon 一重启
    就归零，实测 19:35 重启后 8 秒内连造两条线。
    """
    env = _Env(tmp_path, registered=[1], members=["WARP-WG-1"],
               units={1: "installed"})
    env.last_register = env.now - 60
    got, _ = _run(env)
    assert env.registered_new == []
    assert got == []
    # 过了窗口就能造
    env.last_register = env.now - 700
    got2, _ = _run(env)
    assert env.registered_new == [2]
    assert got2 == [2]


def test_gap_does_not_block_installing_existing_profile(tmp_path):
    """刚注册过的十分钟内，**装单元**照样要做：它不烧 CF 额度。

    把限速写在入口会让「注册了但没单元」的线一起被挡 10 分钟。
    """
    env = _Env(tmp_path, registered=[1, 31], members=["WARP-WG-1"],
               units={1: "installed", 31: "missing"})
    env.last_register = env.now - 5
    got, _ = _run(env)
    assert env.installed == [31]
    assert got == [31]


def test_failed_install_does_not_register_new_device(tmp_path):
    """「已注册没单元」的线装失败时不要退回去注册新设备。

    那等于用烧 CF 额度去绕开一个本地安装故障（/etc 只读、sudo 不通），
    而真正要修的是安装那一步。
    """
    env = _Env(tmp_path, registered=[1, 31], members=["WARP-WG-1"],
               units={1: "installed", 31: "missing"})
    env.install_ok = False
    got, _ = _run(env)
    assert env.registered_new == []
    assert got == []


def test_existing_spare_short_circuits(tmp_path):
    """已经有可用补位线时什么都不做 —— 这是常态路径。"""
    env = _Env(tmp_path, registered=[1, 5], members=["WARP-WG-1"],
               units={1: "installed", 5: "installed"})
    got, _ = _run(env)
    assert got == [5]
    assert env.installed == [] and env.registered_new == []


def test_rate_limit_reads_profile_mtime_not_process_state(tmp_path):
    """限速依据必须是**磁盘上的**注册时刻。

    进程内变量重启即归零，实测 2026-11-04 19:35 restart 后 8 秒内连造
    两条补位线，第二次注册正好撞在 CF 限流窗口上。
    """
    import os as _os
    import time as _time

    env = _Env(tmp_path, registered=[1, 2], members=[], units={})
    box = {"warp_wg": _Wg(env)}
    exec(compile(ast.Module(body=_impl(), type_ignores=[]), "daemon.py", "exec"),
         box)
    fn = box["_last_wg_register_ts"]

    old = _time.time() - 3600
    for i in (1, 2):   # 两份都压旧，才能证明取的是 max
        _os.utime(env.wgdir / f"line{i}" / "profile.conf", (old, old))
    assert abs(fn() - old) < 2, "应返回最新 profile 的 mtime"
    _os.utime(env.wgdir / "line1" / "profile.conf", (_time.time(),) * 2)
    assert fn() > _time.time() - 5
    # 没有注册过任何线 → 0（视为「很久以前」，不阻塞）
    (tmp_path / "empty").mkdir()
    empty = _Env(tmp_path / "empty", registered=[], members=[], units={})
    box2 = {"warp_wg": _Wg(empty)}
    exec(compile(ast.Module(body=_impl(), type_ignores=[]), "daemon.py", "exec"),
         box2)
    assert box2["_last_wg_register_ts"]() == 0.0


def test_hard_cap_stops_runaway_creation(tmp_path):
    """线号硬上限：补位判据哪天回归出 bug 时，宁可停下报错也不无限造线。

    自造线用完即保留（08:00 解封只停止单元），正常总量应停在「并发被封数」
    量级。没有上限时，判据漏认新线会让这条路径每 10 分钟烧一台 CF 设备。
    """
    # 全部在 members 里：没有「有空闲线」的短路，也没「已注册没单元」的
    # 可补（①），只剩「注册新设备」这条路（②）—— 上限必须在这里拦住。
    env = _Env(tmp_path, registered=list(range(1, 65)),
               members=[f"WARP-WG-{i}" for i in range(1, 65)],
               units={i: "installed" for i in range(1, 65)})
    got, _ = _run(env, max_lines=64)
    assert env.registered_new == [], "达到上限后仍在注册新设备"
    assert got == []
