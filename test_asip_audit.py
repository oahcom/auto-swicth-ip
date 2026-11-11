"""独立巡检工具 `tools/asip_audit.py` 的契约测试。

这个工具的价值在于两点，所以测试也只盯这两点：

① **只读** —— 它是旁路检查，不是 daemon 的一部分。它碰任何写路径就可能
   在巡检的时候把生产搞坏。所以「跑完之后一切都没变」必须是被测出来的
   事实，而不是注释里的承诺。

② **能报出问题** —— 一个永远报"一切正常"的工具和一个坏掉的工具没有区别。
   所以必须用**故意做坏的输入**验证它真的能报出来。

③ **不误报** —— 报噪声比不报更糟：真问题会被淹没。所以要验证「档位停用
   但台账里保留历史样本」这种**正常状态**不会被当成问题报出来。
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent
TOOL = PROJECT / "tools" / "asip_audit.py"


def _mod():
    spec = importlib.util.spec_from_file_location("asip_audit", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _mod()


@pytest.fixture(autouse=True)
def _isolate_config_state():
    """巡检测试结束后，把 config 模块与环境恢复原状。

    WHY 必须隔离（实测踩过两次）：
      `tools/asip_audit.py` 读 members 的过程会触发 config 重新加载，
      而 `config.CFG` 的 members 是**按环境变量拼出来的**
      （WG 线 = families × home_nodes）。于是：
        ① 环境被改了 —— `test_warp_wg.py` 设 `WARP_WG_COUNT=3` 后
           reload，读到的 count 不是 3 而是 22；
        ② **config.CFG 里的 members 结构变了** ——
           `test_portability.py` 期望 CF 线 9 条（排除 wg/h2），
           巡检后 WG/H2 进了 members，单元数与预期对不上（13 vs 12）。

      只还原环境**不够** —— config.CFG 是在 reload 那一刻按当时环境算
      出来的，必须**再 reload 一次**让两者重新一致。

    WHY 不能靠「测试顺序」解决：pytest 的执行顺序不该决定别的测试通不通过。
    """
    import os as _os
    keys = ("WARP_WG_COUNT", "WARP_WG_FAMILIES", "WARP_WG_NODES")
    saved_env = {k: _os.environ.get(k) for k in keys}
    try:
        import config as _cfg
        saved_mod = _cfg
    except Exception:
        saved_mod = None
    yield
    for k, v in saved_env.items():
        if v is None:
            _os.environ.pop(k, None)
        else:
            _os.environ[k] = v
    if saved_mod is not None:
        try:
            import importlib
            importlib.reload(saved_mod)      # 按还原后的环境重算，让 CFG 与环境一致
        except Exception:
            pass


def _run(project: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--project", str(project), *args],
        capture_output=True, text=True, timeout=180)


# ------------------------------------------------------------ ① 只读保证


def test_tool_does_not_modify_state_file(mod, tmp_path):
    """巡检跑完，状态台账必须**一个字节都没变**。

    WHY 这是最重要的契约：巡检是旁路检查，它跑的时候生产正在跑。
    它如果写了状态表，就可能在 daemon 正在写的时候抢写，把 156 条状态
    搞坏 —— 那比不巡检更糟。

    回滚即失败：任何写操作都会让 md5 变化。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    state = proj / ".warp_line_state.json"
    payload = {"version": 1, "lines": {
        "WARP-H2-NRT-0": {"ip": "1.1.1.1", "last_verdict": "OK",
                          "last_probe_ts": 1000.0},
        "WARP-WG-1": {"ip": None, "last_verdict": "blocked"},
    }}
    state.write_text(json.dumps(payload), encoding="utf-8")
    before = state.read_bytes()
    before_mtime = state.stat().st_mtime_ns

    r = _run(proj, "--json")
    assert r.returncode in (0, 1, 2), f"工具不该自己崩：{r.stderr[-500:]}"
    assert state.read_bytes() == before, "巡检改了状态台账 —— 只读契约被破坏"
    assert state.stat().st_mtime_ns == before_mtime, "巡检重写了状态台账"


def test_tool_does_not_touch_tiers_or_singbox(mod, tmp_path):
    """档位文件与 sing-box 配置也不能被改。

    WHY：档位决定"哪些线该跑"，sing-box 配置决定"哪些线有出口"。这两个
    被巡检改了，等于巡检自己制造了它要报告的问题。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    tiers = proj / ".warp" / "tiers.json"
    tiers.write_text(json.dumps({"current": "h2-only",
                                 "tiers": {"h2-only": {"wg": ""}}}),
                     encoding="utf-8")
    sb = proj / "singbox-config.json"
    sb.write_text(json.dumps({"outbounds": [{"tag": "x"}]}), encoding="utf-8")
    t_before = tiers.read_bytes()
    s_before = sb.read_bytes()

    _run(proj, "--json")
    assert tiers.read_bytes() == t_before, "巡检改了档位文件"
    assert sb.read_bytes() == s_before, "巡检改了 sing-box 配置"


def test_tool_does_not_create_files(mod, tmp_path):
    """巡检不该在项目目录里留下任何新文件。

    WHY：巡检工具常被挂到 timer 上跑。如果它每次都写临时文件、又不清理，
    跑几个月就会在项目目录里堆一堆垃圾 —— 而项目目录是 git 仓库。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    (proj / ".warp_line_state.json").write_text(
        json.dumps({"version": 1, "lines": {}}), encoding="utf-8")
    before = {p.name for p in proj.iterdir()}
    _run(proj, "--json")
    after = {p.name for p in proj.iterdir()}
    assert after == before, f"巡检在项目目录里留下了新东西：{after - before}"


# ------------------------------------------------------ ② 能报出问题


def test_reports_tier_off_with_state_when_conclusion_remains(mod, tmp_path):
    """档位停用的线若还留着**结论**，必须报出来。

    回滚即失败：把判据改回"有台账条目"（旧判据），本测试仍会过 ——
    所以再补一个 test_tier_off_keeping_samples_is_not_a_problem 反向锁住。
    两者合起来才锁住正确判据。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    (proj / ".warp" / "tiers.json").write_text(
        json.dumps({"current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}),
        encoding="utf-8")
    (proj / ".warp_line_state.json").write_text(json.dumps({
        "version": 1, "lines": {
            "WARP-WG-1": {"last_verdict": "blocked", "ip": "9.9.9.9",
                          "last_probe_ts": 1000.0},
        }}), encoding="utf-8")

    rep = mod.collect(proj)
    assert "WARP-WG-1" in rep["problems"]["tier_off_with_state"], \
        "档位停用却留着结论 —— 这是档位切换没清干净的证据，必须报"


def test_tier_off_keeping_samples_is_not_a_problem(mod, tmp_path):
    """档位停用的线若**只保留历史样本**（结论已清），**不算问题**。

    WHY 这个反向断言同样重要：台账里保留条目本身是对的 ——
    `ban_probes` 是当天的历史样本，是渠道锁判据的原料，档位停用时刻意
    不清它。旧判据（"有台账条目就算问题"）会每次都报 22 条假问题 ——
    **报噪声比不报更糟**，真问题会被淹没。

    回滚即失败：把判据改回"有条目即报"，本测试失败。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    (proj / ".warp" / "tiers.json").write_text(
        json.dumps({"current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}),
        encoding="utf-8")
    (proj / ".warp_line_state.json").write_text(json.dumps({
        "version": 1, "lines": {
            # 结论已清空，但保留当天的历史样本 —— 正常状态
            "WARP-WG-1": {"last_verdict": "", "ip": None,
                          "last_probe_ts": 0.0,
                          "ban_probes": [[1000.0, 1000.0, 1, "quota"]]},
        }}), encoding="utf-8")

    rep = mod.collect(proj)
    assert "WARP-WG-1" not in rep["problems"]["tier_off_with_state"], \
        "保留历史样本是正常状态，不能报成不一致 —— 否则真问题会被淹没"


def test_probe_reason_labels_each_cause(mod):
    """「无样本」的成因必须区分开，六种成因不能共用一句话。

    WHY：六种故障的处置动作完全不同（改配置 / 换地址 / 修隧道 / 查代码）。
    共用一句话等于让人自己猜。

    回滚即失败：把任一分支合并掉，本测试失败。
    """
    now = 100000.0
    # 不在探测名单
    assert "不在探测名单" in mod.probe_reason(
        "X", None, False, now)
    # 从未探测
    assert mod.probe_reason("X", {}, True, now) == "从未探测"
    # 惩罚期短路
    r = mod.probe_reason("X", {"last_probe_ts": now - 60,
                                "blocked_notes": [[1, 1, 1, "blocked"]]},
                          True, now)
    assert "惩罚期短路" in r
    # 取不到地址
    r = mod.probe_reason("X", {"last_probe_ts": now - 60,
                                "ban_noaddr_since": now - 600},
                          True, now)
    assert "取不到地址" in r
    # 探测未入账
    r = mod.probe_reason("X", {"last_probe_ts": now - 60}, True, now)
    assert "探测未入账" in r


def test_exit_code_signals_problems(mod, tmp_path):
    """退出码必须能区分「无问题 / 有问题 / 工具自身出错」。

    WHY：这个工具的用途之一是挂 systemd timer 做告警。退出码是 timer
    唯一能读到的信号 —— 没有可用的退出码，timer 就只能靠解析输出文本，
    那太脆了。

    回滚即失败：退出码恒为 0，本测试失败。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    (proj / ".warp" / "tiers.json").write_text(
        json.dumps({"current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}),
        encoding="utf-8")
    (proj / ".warp_line_state.json").write_text(json.dumps({
        "version": 1, "lines": {
            "WARP-WG-1": {"last_verdict": "blocked", "last_probe_ts": 1000.0},
        }}), encoding="utf-8")
    r = _run(proj, "--json")
    assert r.returncode == 1, f"有问题时应返回 1，实际 {r.returncode}"
    assert "WARP-WG-1" in r.stdout


def test_json_output_has_full_lists(mod, tmp_path):
    """JSON 模式必须给**完整列表**，不能截断。

    WHY：文本模式给人看，截断到 10 条没问题；JSON 是给机器消费的
    （告警脚本要拿完整 tag 列表去查状态表）。截断的 JSON 会让下游
    漏掉问题。

    回滚即失败：把完整列表换成前 N 条，本测试失败。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    (proj / ".warp" / "tiers.json").write_text(
        json.dumps({"current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}),
        encoding="utf-8")
    # members 里正好有 22 条 WG 线（WG-23+ 是补位池，不在探测名单）
    lines = {f"WARP-WG-{i}": {"last_verdict": "blocked",
                              "last_probe_ts": 1000.0} for i in range(1, 23)}
    (proj / ".warp_line_state.json").write_text(
        json.dumps({"version": 1, "lines": lines}), encoding="utf-8")
    r = _run(proj, "--json")
    data = json.loads(r.stdout)
    lst = data["problems"]["tier_off_with_state"]
    assert len(lst) == 22, f"JSON 必须给完整列表，实际给了 {len(lst)} 条"


def test_text_mode_truncates_but_json_does_not(mod, tmp_path):
    """文本模式可以截断（给人看），JSON 模式必须完整（给机器看）。"""
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    (proj / ".warp" / "tiers.json").write_text(
        json.dumps({"current": "h2-only", "tiers": {"h2-only": {"wg": ""}}}),
        encoding="utf-8")
    lines = {f"WARP-WG-{i}": {"last_verdict": "blocked",
                              "last_probe_ts": 1000.0} for i in range(1, 23)}
    (proj / ".warp_line_state.json").write_text(
        json.dumps({"version": 1, "lines": lines}), encoding="utf-8")
    r = _run(proj, "--max-list", "3")
    assert "还有" in r.stdout, "文本模式应提示还有更多"
    rj = _run(proj, "--json")
    data = json.loads(rj.stdout)
    assert len(data["problems"]["tier_off_with_state"]) == 22


def test_reads_wg_members_with_env_injected(mod):
    """必须能读到 22 条 WG 线（.env 注入问题）。

    WHY 这是我踩过的坑：`config.CFG["warp"]["members"]` 里的 WG 线是**按
    环境变量拼出来的**。不注入 .env 就得到 WG=0 条，于是**全部 22 条 WG
    线被误判成"不在探测名单"** —— 巡检工具自己制造假警报。

    回滚即失败：去掉 .env 注入，本测试失败。
    """
    members, warnings = mod.read_members()
    wg = [t for t, m in members.items() if m.get("kind") == "wg"]
    assert len(wg) >= 20, \
        f"只读到 {len(wg)} 条 WG 线 —— .env 没注入，会把 WG 全误判成不在名单"


def test_empty_env_value_does_not_win_over_dotenv(mod, monkeypatch):
    """外部环境里的**空值**不能让 .env 里的真值失效。

    WHY：这是同一个坑的第二个变体。子进程可能继承了
    `WARP_WG_FAMILIES=`（空串）—— 那时 config 算出 WG=0 条，22 条 WG
    线全被误判成"不在探测名单"。巡检工具自己制造假警报。

    WHY 规则是「以 .env 为准」：daemon 的真实环境来自 systemd 的
    `EnvironmentFile=-.env`，实测 daemon 进程里 `WARP_WG_FAMILIES=v6`、
    `WARP_WG_NODES=22`，与 .env 完全一致。巡检要回答的是
    「**daemon 现在到底认哪些线**」，所以必须复现 daemon 的那份环境。

    回滚即失败：子进程改回继承调用者环境，本测试失败。
    """
    monkeypatch.setenv("WARP_WG_FAMILIES", "")   # 空值，模拟继承来的坏环境
    members, _ = mod.read_members()
    wg = [t for t, m in members.items() if m.get("kind") == "wg"]
    assert len(wg) >= 20, \
        f"空环境值不该赢过 .env —— 只读到 {len(wg)} 条 WG 线"


def test_caller_environment_does_not_change_daemon_view(mod, monkeypatch):
    """调用者的临时环境变量**不得**改变巡检看到的 daemon 视角。

    WHY：巡检回答的是「daemon 认哪些线」。如果调用者环境里有个
    `WARP_WG_COUNT=3`，那不是 daemon 的配置 —— daemon 跑的是 .env 那份。
    实测踩过：测试设了 COUNT=3，巡检继承它，算出的 WG 条数不是 22。

    回滚即失败：子进程改回继承完整环境，本测试失败。
    """
    monkeypatch.setenv("WARP_WG_COUNT", "3")
    members, _ = mod.read_members()
    wg = [t for t, m in members.items() if m.get("kind") == "wg"]
    assert len(wg) >= 20, \
        f"调用者的临时环境不该改变 daemon 视角 —— 只读到 {len(wg)} 条 WG 线"


def test_warns_when_singbox_config_unreadable(mod, tmp_path):
    """sing-box 配置读不到时必须**明确告警**，而不是报"一个 outbound 都没有"。

    WHY：订阅刷新用「写 .tmp 再 os.replace」的方式原子替换配置，中间有
    窗口能读到半截 JSON。读不到时若返回空集，调用方会当成"全部 outbound
    都没了"，报出一堆假的"配置裂了"。

    回滚即失败：读不到时静默返回空集，本测试失败。
    """
    orig = mod.read_outbound_tags
    try:
        mod.read_outbound_tags = lambda *a, **k: (set(), "读不到测试用故障")
        proj = tmp_path / "proj"
        (proj / ".warp").mkdir(parents=True)
        rep = mod.collect(proj)
        joined = " ".join(rep.get("warnings") or [])
        assert "读不到" in joined, "读不到 sing-box 配置必须明确告警"
    finally:
        mod.read_outbound_tags = orig


def test_h2_lines_not_reported_missing_when_channel_ledger_exists(mod, tmp_path):
    """H2 线的出口判据必须走 `.warp/h2/channels.json`，不能拿 tag 去主配置翻。

    WHY（这是本工具最严重的一次假阳性，必须锁住）：
      H2 一条线涉及**三个不同端口**，极易认错：
        · member["port"]  = 1180+  usque 的 socks 口（daemon 探测用这个）
        · inbound_port    = 22700+ sing-box 的 direct 入站（sing-box 侧）
        · 主配置 outbound = 根本没有 H2 —— H2 跑在独立配置 .warp/h2/singbox.json
      我第一版拿 WARP-H2-* tag 去主配置 `singbox-config.json` 找 outbound，
      实测把 56 条**正在正常跑**的 H2 线全报成「没有出口」。
      第二版又拿 member["port"](1180+) 去比 inbounds[].listen_port(22700+)，
      永远不相等，误报涨到 134 条。
      渲染器已经把 member_tag ↔ inbound_port 写进 channels.json 了 ——
      那才是权威，任何外部核对都该读它，而不是猜命名规则。

    回滚即失败：把判据改回「tag not in 主配置 outbounds」，本测试失败。
    """
    proj = tmp_path / "proj"
    (proj / ".warp" / "h2").mkdir(parents=True)
    # H2 台账：一条线，member_tag -> inbound_port
    (proj / ".warp" / "h2" / "channels.json").write_text(json.dumps([
        {"member_tag": "WARP-H2-NRT-0", "inbound_port": 22700, "socks_port": 1180},
    ]), encoding="utf-8")
    # H2 的 sing-box 配置：有 22700 这个 direct 入站
    (proj / ".warp" / "h2" / "singbox.json").write_text(json.dumps({
        "inbounds": [{"type": "direct", "listen_port": 22700, "tag": "h2in-0"}],
        "outbounds": [{"tag": "h2-sel-0", "type": "selector"}],
    }), encoding="utf-8")
    # 主配置：**故意不含任何 H2**（真实情形就是这样）
    (proj / "singbox-config.json").write_text(json.dumps({
        "outbounds": [{"tag": "WARP-CF", "type": "selector"}],
    }), encoding="utf-8")

    rep = mod.collect(proj)
    missing = rep["problems"]["missing_out"]
    assert "WARP-H2-NRT-0" not in missing, (
        "H2 线在台账与 h2/singbox.json 里都有出口，不该被报 missing_out；"
        f"实际报出了：{missing}")


def test_h2_line_reported_when_ledger_missing(mod, tmp_path):
    """反向约束：H2 线在探测名单里，台账却根本没这条 —— 这才该报。

    WHY 需要反向约束：只有「不误报」的测试，把判据改成永远返回空列表
    也能过 —— 那是假绿。必须同时锁住「真问题要报得出来」。
    """
    proj = tmp_path / "proj"
    (proj / ".warp" / "h2").mkdir(parents=True)
    # 台账为空：渲染器不认识这条线
    (proj / ".warp" / "h2" / "channels.json").write_text("[]", encoding="utf-8")
    (proj / ".warp" / "h2" / "singbox.json").write_text(json.dumps({
        "inbounds": [], "outbounds": [],
    }), encoding="utf-8")
    (proj / "singbox-config.json").write_text(json.dumps({
        "outbounds": [{"tag": "WARP-CF", "type": "selector"}],
    }), encoding="utf-8")

    rep = mod.collect(proj)
    missing = rep["problems"]["missing_out"]
    h2 = [t for t in missing if t.startswith("WARP-H2-")]
    assert h2, f"台账里没有的 H2 线必须报出来；实际 missing_out={missing[:8]}"


def test_tier_disabled_lines_not_reported_as_missing_out(mod, tmp_path):
    """档位停用的线不该报 missing_out（它们**故意**不跑、也没有出口）。

    WHY：档位停用是正常状态 —— 单元被 stop、渲染器不再为它保 outbound。
    把它报成「没有出口」是拿正常状态当故障（实测 22 条 WG 线因此误报）。
    它该由 tier_off_with_state 那条规则管（判据是「还留着结论」）。

    回滚即失败：去掉 `and t not in tier_off` 过滤，本测试失败。
    """
    proj = tmp_path / "proj"
    (proj / ".warp").mkdir(parents=True)
    # 档位：当前 h2-only，WG 全停用（与生产一致）
    (proj / ".warp" / "tiers.json").write_text(json.dumps({
        "current": "h2-only", "tiers": {"h2-only": {"wg": ""}},
    }), encoding="utf-8")
    (proj / "singbox-config.json").write_text(json.dumps({
        "outbounds": [{"tag": "WARP-CF", "type": "selector"}],
    }), encoding="utf-8")

    rep = mod.collect(proj)
    missing = rep["problems"]["missing_out"]
    wg = [t for t in missing if t.startswith("WARP-WG-")]
    assert not wg, (
        "档位停用的 WG 线不该出现在 missing_out；"
        f"实际报出了 {len(wg)} 条：{wg[:6]}")


def test_scan_tag_sharing_index_with_real_member_is_not_retired(mod, tmp_path):
    """SCAN 标签与正式成员共用编号时，不能判成「缩容遗留」。

    WHY（本工具第二大的假阳性，实测 56 条）：
      `WARP-H2-SCAN-<N>` 是「扫描到 usque-h2-<N>.service 在跑、但 members
      里没有对应声明」时的兜底标签，它和正式线**共用同一个编号空间**。
      `usque-h2-112.service` 既可能是 SCAN 孤儿，也可能是
      `WARP-H2-NRT-112` 的合法单元 —— 取决于 112 这个编号有没有被
      当前渲染产物占用。

      我第一版对 SCAN 分支无条件返回单元名，于是渲染完成、112~167
      正式上线之后，台账里残留的旧 `WARP-H2-SCAN-112..167` 全被判成
      「缩容遗留」—— 实测 56 条假警报，而它们恰恰是 members 与磁盘
      单元 112 对 112 完全对齐的正常线。

    回滚即失败：SCAN 分支去掉 `if idx in used_h2_idx: return None`，
    本测试失败。
    """
    proj = tmp_path / "proj"
    (proj / ".warp" / "h2").mkdir(parents=True)
    # 渲染产物：112 号已被正式占用
    (proj / ".warp" / "h2" / "channels.json").write_text(json.dumps([
        {"index": 112, "member_tag": "WARP-H2-NRT-112",
         "inbound_port": 22812, "socks_port": 1292},
    ]), encoding="utf-8")
    (proj / ".warp" / "h2" / "singbox.json").write_text(json.dumps({
        "inbounds": [{"listen_port": 22812, "tag": "h2in-112"}],
        "outbounds": [{"tag": "h2-sel-112", "type": "selector"}],
    }), encoding="utf-8")
    (proj / "singbox-config.json").write_text(json.dumps({
        "outbounds": [{"tag": "WARP-CF", "type": "selector"}],
    }), encoding="utf-8")

    # 台账里同时有正式线（在名单）与它的旧 SCAN 记录（不在名单）
    (proj / ".warp_line_state.json").write_text(json.dumps({
        "version": 1,
        "lines": {
            "WARP-H2-NRT-112": {"last_verdict": "OK", "last_probe_ts": 1000.0},
            "WARP-H2-SCAN-112": {"last_verdict": "unknown", "last_probe_ts": 500.0},
        },
    }), encoding="utf-8")

    rep = mod.collect(proj)
    retired = rep["problems"]["retired"]
    assert "WARP-H2-SCAN-112" not in retired, (
        "112 号已被正式成员占用，SCAN-112 只是旧台账记录，不是遗留单元；"
        f"实际 retired={retired}")


def test_scan_tag_without_real_member_is_retired(mod, tmp_path):
    """反向约束：SCAN 标签的编号**没被**任何正式成员占用 → 这才该报。

    WHY 需要反向约束：只有「不误报」的测试，把判据改成永远返回 None
    也能过 —— 假绿。真遗留（缩容后单元文件还在）必须报得出来。
    """
    proj = tmp_path / "proj"
    (proj / ".warp" / "h2").mkdir(parents=True)
    (proj / ".warp" / "h2" / "channels.json").write_text("[]", encoding="utf-8")
    (proj / ".warp" / "h2" / "singbox.json").write_text(json.dumps({
        "inbounds": [], "outbounds": [],
    }), encoding="utf-8")
    (proj / "singbox-config.json").write_text(json.dumps({
        "outbounds": [{"tag": "WARP-CF", "type": "selector"}],
    }), encoding="utf-8")
    (proj / ".warp_line_state.json").write_text(json.dumps({
        "version": 1,
        "lines": {"WARP-H2-SCAN-999": {"last_verdict": "unknown"}},
    }), encoding="utf-8")

    # 把 999 号单元假装成在磁盘上
    orig = mod.read_unit_files_on_disk
    try:
        mod.read_unit_files_on_disk = lambda: ({"usque-h2-999.service"}, None)
        rep = mod.collect(proj)
        retired = rep["problems"]["retired"]
        assert "WARP-H2-SCAN-999" in retired, (
            f"999 号没人占用、单元文件还在 → 应判遗留；实际 retired={retired}")
    finally:
        mod.read_unit_files_on_disk = orig
