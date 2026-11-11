#!/usr/bin/env python3
"""跨源巡检：一条线路的「真相」分散在 5 个源，这里把它们摆到一张表上对账。

WHY 需要这个工具（为什么不能继续放在 daemon 的启动关键路径上）：

    线路的真相分散在 5 个地方 ——
      1. `config.CFG["warp"]["members"]`      探测名单（daemon 实际会去探的线）
      2. sing-box 配置文件的 outbounds          渲染产物（哪些线真的有出口配置）
      3. systemd 单元                            进程事实（哪些隧道真在跑）
      4. `.warp_line_state.json`                 状态台账（每条线的上次结论）
      5. `.warp/tiers.json`                      档位（哪些 WG 线该跑）
    没有任何一处校验其余几处，于是出现**半开状态**：文件里有线、daemon 看不见。
    实测 2026-11-11：状态台账 156 条 tag，只有 87 条在 members 里；22 条 WG 线的
    结论冻结了 27~33 小时。

    `daemon.py` 里已有 `_config_consistency_check()` 做这件事，但它**出过一次生产
    事故**：2026-11-11 17:22:31 起，daemon 因该函数内部引用未定义的名字 `PROJECT`
    抛 NameError，systemd `Restart=always` 反复拉起，累计 23 次重启循环，17:27 才
    恢复。**一个只读的告警检查，挂在 daemon 的启动路径上，失败时却能把整个代理
    服务掀翻** —— 这是把「诊断」和「生产」耦合在一起的经典代价。

    所以这个工具把跨源核对从 daemon 的启动路径上**拿出来**，做成一个可以随时手工
    跑、可以挂 systemd timer、**失败只报警不掀桌**的旁路检查：
      - 退出码 0 = 无问题，1 = 有问题（可挂 timer / 告警），2 = 工具自身出错。
      - 它**不 import daemon**（daemon 顶层 import 有副作用：读 .env、可能做
        初始化），只 import `config`（读 CFG）与 `warp_blocks`（读档位）。

只读保证（本工具**只读不改**）：

    - 只读文件：members（经 config）、sing-box 配置、`.warp_line_state.json`、
      `.warp/tiers.json`、`.warp/h2/channels.json`。
    - 只读 systemctl 查询：`list-units`（看在跑的）、`list-unit-files`（看单元
      文件还在不在磁盘上）。**不执行任何 start/stop/enable/disable/restart。**
    - 只读进程信息：不 `ps` 也不 `pgrep`，进程事实全部来自 systemd。
    - **不写任何状态文件**：不碰 `.warp_line_state.json`、不碰 `.warp/`、不碰
      sing-box 配置、不碰 systemd。
    - 不 import daemon、不调用 daemon 里任何会写状态的函数。

用法：
    python3 tools/asip_audit.py                 # 人类可读报告
    python3 tools/asip_audit.py --json          # 机器可读 JSON
    python3 tools/asip_audit.py --project /path # 指定项目根（默认脚本的父目录）
    python3 tools/asip_audit.py --max-list 30   # 文本报告里每类列举几个（默认 10）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 项目根定位 + .env 注入
# ---------------------------------------------------------------------------
# WHY 必须自己注入 .env 再 import config：`config.CFG["warp"]["members"]` 里
# WG 线是**按环境变量拼出来的**（`WARP_WG_FAMILIES` × `WARP_WG_NODES`）。
# 不注入 .env 就 import config，members 里 WG=0 条 —— 22 条 WG 线会被全部
# 误判成「不在探测名单」，报告直接变成假警报。
# daemon 走 systemd 的 `EnvironmentFile=-.env` 拿到这些变量；手跑没有这条
# 路径，所以这里自己解析一份（不 shell source，避免执行任意命令）。
PROJECT = Path(__file__).resolve().parent.parent

# `--project` 必须在 import config 之前就生效：项目根决定 `.env` 从哪读、
# `config`/`warp_blocks` 从哪 import。所以这里先做一次**轻量**的 argv 预扫
# （不建 argparse，避免和后面重复解析），再走正经的 argparse。
#
# WHY 代码目录与数据目录要分开（2026-11-11 修正）：
#   `--project` 的用途是「让工具去读**另一份**项目目录的数据」—— 比如
#   拿一份台账副本做复盘、或者在演练环境里跑。**代码**（config.py /
#   warp_blocks.py）必须始终从**工具自己所在的项目** import：
#   指向一个临时目录时，那目录里没有 config.py，import 直接 ModuleNotFound
#   （实测踩过）。
#   所以：代码目录 = 工具所在项目（固定），数据目录 = --project 指定的
#   （可变，默认就是工具所在项目）。
SCRIPT_DIR = Path(__file__).resolve().parent.parent


def _early_project() -> Path:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        try:
            if a == "--project" and i + 1 < len(argv):
                return Path(argv[i + 1]).resolve()
            if a.startswith("--project="):
                return Path(a.split("=", 1)[1]).resolve()
        except OSError:
            # `Path.resolve()` 在无权限的路径上会抛（如 `/proc/1/root`）。
            # 这里不让它炸在模块层 —— 后面 `collect()` 会把「读不到」如实
            # 报成警告，而不是让调用方只看到一个 Python traceback。
            return SCRIPT_DIR
    return SCRIPT_DIR


try:
    PROJECT = _early_project()
except Exception as _proj_err:  # noqa: BLE001 — 工具自身出错 → 退出码 2
    print(f"asip_audit: 工具自身出错：--project 路径无法解析：{_proj_err}",
          file=sys.stderr)
    sys.exit(2)
CODE_DIR = SCRIPT_DIR
sys.path.insert(0, str(CODE_DIR))

_env_file = CODE_DIR / ".env"
try:
    _env_lines = (_env_file.read_text(encoding="utf-8").splitlines()
                  if _env_file.exists() else [])
except OSError as _env_err:
    print(f"asip_audit: 工具自身出错：读不到 {CODE_DIR / '.env'}：{_env_err}",
          file=sys.stderr)
    sys.exit(2)

# WHY **不在模块级**注入 .env（2026-11-11 修正）：
#   早期版本在这里就把 .env 灌进 os.environ。实测踩过：宿主进程
#   （例如测试套件）import 这个工具后，环境里就多了 WARP_WG_FAMILIES /
#   WARP_WG_NODES —— 于是宿主随后 import config 时，members 里会多出
#   22 条 WG 线和一堆 H2 线，宿主自己的断言全乱
#   （test_portability 期望 CF 线 9 条，实际算成 13 条）。
#
#   这个工具是**只读巡检**：它不该给宿主留下任何环境副作用。
#   .env 的值只在**子进程**里用（见 `_read_members_in_subprocess`），
#   那才是 daemon 真正使用的那份配置。
#   这里仍然把 .env 读进内存，只是为了在需要时取用，不改环境。

try:
    import warp_blocks  # noqa: E402
    from config import CFG  # noqa: E402
except Exception as _imp_err:  # noqa: BLE001 — 工具自身出错 → 退出码 2
    # WHY 这里就退出而不是往上抛：退出码 2 = 「工具自身出错」，挂 timer
    # 的调用方要能把它和「查出了问题」（退出码 1）分开。import config 失败
    # 通常意味着项目路径不对（`--project` 指错了），不是「查出了问题」。
    print(f"asip_audit: 工具自身出错：无法导入 config/warp_blocks："
          f"{_imp_err}", file=sys.stderr)
    sys.exit(2)

# 端口/标签契约（照抄 daemon.py `_scan_running_tunnels` 与 config.py 注释）：
#   usque-h2-<N>.service → 端口 1180+N，tag `WARP-H2-SCAN-<N>`
#   warp-wg-<N>.service  → 端口 1110+N-1，tag `WARP-WG-<N>`
#   usque-warp-<N>.service → **没有正则兜底**：只有 members 里声明过的才能
#     定出 tag（daemon 就是这么做的）。
H2_UNIT_RE = re.compile(r"usque-h2-(\d+)\.service")
WG_UNIT_RE = re.compile(r"warp-wg-(\d+)\.service")
SCAN_TAG_RE = re.compile(r"WARP-H2-SCAN-(\d+)")
H2_COLO_TAG_RE = re.compile(r"WARP-H2-(?:[A-Z]+)-(\d+)")
WG_TAG_RE = re.compile(r"WARP-WG-(\d+)")

TUNNEL_UNIT_GLOBS = ("usque-h2-*.service", "warp-wg-*.service", "usque-warp-*.service")


# ---------------------------------------------------------------------------
# 只读采集
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    """跑一条只读命令。返回 `(ok, stdout)`；失败时 ok=False 且 stdout 为空。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    if r.returncode != 0:
        return False, (r.stdout or "")
    return True, (r.stdout or "")


def _apply_dotenv_defaults() -> dict[str, str]:
    """把 .env 里的值补进环境（**空值让 .env 赢**，非空值不覆盖）。

    WHY 要在 `read_members()` 里再调一次（而不是只在 import 时调）：
      导入时的那一次只覆盖了「进程启动那一刻」的环境。之后调用方
      （测试、嵌入式使用）可能又把某个变量设成了空串 —— 那时
      config 重载算出来的 WG 就是 0 条，于是 22 条 WG 线被全部误判成
      「不在探测名单」。巡检工具自己制造假警报。
      实测踩过：子进程继承了空的 `WARP_WG_FAMILIES=`。

    WHY 规则是「外部**非空**值优先，**空值**让 .env 赢」：
      daemon 在 systemd 单元里显式声明的 `Environment=` 才是权威值，
      .env 只是默认；空串不可能是有意的配置。

    WHY 必须先于 `importlib.reload(config)`：config 的 members 拼装
    是在 import 时按环境变量算的，环境没修好，重载也没用。

    WHY 返回「补了哪些键」而不是就地改完就算：这个工具是**只读巡检**，
    不该给宿主进程留下环境副作用。调用方用它决定要不要还原。
    """
    applied: dict[str, str] = {}
    env_file = CODE_DIR / ".env"
    if not env_file.exists():
        return applied
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if os.environ.get(k):
            continue                    # 外部已有非空值 = 权威值
        os.environ[k] = v                # 空值 → 让 .env 赢
        applied[k] = v
    return applied                # 空值 → 让 .env 赢


def _read_members_in_subprocess() -> tuple[dict[str, dict], list[str]]:
    """在**独立子进程**里读探测名单，把结果作为 JSON 拿回来。

    WHY 必须在子进程里读（2026-11-11 定案）：
      `config.CFG["warp"]["members"]` 里的 WG 线是**按环境变量拼出来的**
      （`_append_wg_members`：`len(families) × home_nodes`）。要拿到权威的
      22 条，必须让 config 在**环境变量就位之后**重新 import。

      但在本进程里做这件事会**污染宿主**：
        - `importlib.reload(config)` 换掉全局的 `config.CFG`；
        - 顺带改环境变量，又得还原；还原后 config 又与环境不一致，
          再 reload 一次 —— 于是「宿主看到的 config」和「宿主自己的环境」
          永远对不上。
      实测踩过：巡检跑完后 `test_warp_wg.py` 的两个测试失败（它用
      `WARP_WG_COUNT` 验证「环境变量变化 → 配置重新生成」）。

      子进程把这件事完全隔离：宿主的环境与模块对象一个字节都不动，
      而子进程内部可以放心地设环境、reload、再读。

    WHY 子进程用**纯净环境**而不是继承调用者的：
      daemon 的真实环境是 systemd 给的 —— `EnvironmentFile=-.env` 注入
      `.env` 的值，单元里没有再覆盖（实测：daemon 进程里
      `WARP_WG_FAMILIES=v6`、`WARP_WG_NODES=22`，与 .env 完全一致）。
      所以「daemon 会用哪份配置」的权威答案就是 **.env 里的那份**。

      如果子进程继承调用者的环境，就会拿到**调用者临时设的值** ——
      实测踩过：测试里设了 `WARP_WG_COUNT=3`，巡检子进程继承它，
      于是算出的 WG 条数不是 22。而巡检要回答的是
      「**daemon 现在到底认哪些线**」，不是「调用者此刻想要什么」。

      所以：子进程用**最小环境**（PATH/HOME/LANG + .env 的值），
      .env 的值**强制生效**（不是"外部非空优先"）—— 这才与 daemon 一致。

    WHY 不多开一个文件：用 `python3 -c` 避免在项目目录里落临时文件
    （这工具是只读的，不该留痕迹）。
    """
    warnings: list[str] = []
    code = (
        "import json,os,sys\n"
        f"sys.path.insert(0,{str(CODE_DIR)!r})\n"
        f"_env={str(CODE_DIR / '.env')!r}\n"
        "if os.path.exists(_env):\n"
        "    for _l in open(_env,encoding='utf-8').read().splitlines():\n"
        "        _l=_l.strip()\n"
        "        if not _l or _l.startswith('#') or '=' not in _l: continue\n"
        "        _k,_v=_l.split('=',1); _k=_k.strip(); _v=_v.strip()\n"
        "        if len(_v)>=2 and _v[0]==_v[-1] and _v[0] in '\\\"\\'': _v=_v[1:-1]\n"
        "        os.environ[_k]=_v\n"          # 强制生效：与 daemon 的 .env 一致
        "import config\n"
        "print(json.dumps(config.CFG.get('warp',{}).get('members') or [],"
        "ensure_ascii=False))\n"
    )
    # 最小环境：PATH/HOME/LANG 够跑 python 与读文件，其余全部丢弃 ——
    # 调用者的临时环境变量（WARP_WG_COUNT 等）不得影响 daemon 的配置。
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL")
           if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        warnings.append(f"members 读取子进程失败：{e}")
        return {}, warnings
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        warnings.append(f"members 读取子进程无输出（rc={r.returncode}）")
        return {}, warnings
    try:
        members = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError) as e:
        warnings.append(f"members 读取子进程输出解析失败：{e}")
        return {}, warnings
    if not isinstance(members, list):
        warnings.append("members 读取子进程返回的不是列表")
        return {}, warnings
    out_map: dict[str, dict] = {}
    for m in members:
        if isinstance(m, dict) and m.get("tag"):
            out_map[str(m["tag"])] = m
    return out_map, warnings


def read_members() -> tuple[dict[str, dict], list[str]]:
    """探测名单（config.CFG["warp"]["members"]）。返回 ({tag: member}, 警告列表)。

    WHY 用子进程读：见 `_read_members_in_subprocess` 的 WHY —— 在本进程
    里 reload config 会污染宿主的模块与环境，而 22 条 WG 线又必须靠
    「环境就位后重新 import config」才拿得到。
    """
    warnings: list[str] = []
    out, w = _read_members_in_subprocess()
    warnings += w
    if not out:
        warnings.append("members 为空：探测名单读不到或配置异常")
    return out, warnings


def read_outbound_tags(project: Path) -> tuple[set[str], str | None]:
    """sing-box 配置里的 outbound tag 集合。

    从 `CFG["singbox"]["config_file"]` 读（主文件为准；slot2..slot6 只是
    热切换副本，不参与对账）。`--project` 指向别的数据目录时优先用那一份
    `singbox-config.json`，没有才回落到 CFG 声明的路径。

    读不到时返回**空集 + 错误说明**，而不是抛异常 —— 这份文件由订阅刷新
    流程原子替换，读到半截 JSON 是真实可能；空集会被调用方当成「一个
    outbound 都没有」，从而报出一堆假的「配置裂了」。
    """
    path = CFG.get("singbox", {}).get("config_file")
    override = project / "singbox-config.json"
    if override.exists():
        path = str(override)
    if not path:
        return set(), "sing-box 配置路径未声明（CFG['singbox']['config_file'] 为空）"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        return set(), f"读不到 sing-box 配置 {path}：{e}"
    except ValueError as e:
        return set(), f"sing-box 配置解析失败 {path}：{e}"
    outs = data.get("outbounds") if isinstance(data, dict) else None
    if not isinstance(outs, list):
        return set(), f"sing-box 配置 {path} 里没有 outbounds 数组"
    return {str(o.get("tag")) for o in outs
            if isinstance(o, dict) and o.get("tag")}, None


def read_h2_selector_tags() -> tuple[set[str], str | None]:
    """H2 专用 sing-box 配置里的 selector tag 集合。

    WHY H2 必须单独查一份配置（这是本工具最初最大的假阳性）：
      H2 线与 CF/WG 线的接法完全不同 ——
        · CF/WG：线本身就是一个 outbound，tag 直接是 `WARP-CF` / `WARP-WG-1`
        · H2  ：usque 提供 1180+ 的 socks 口，sing-box 里对应的是
                `h2-sel-<n>` selector + 22700+ 的 direct 入站，
                共用一份**独立**的配置 `.warp/h2/singbox.json`
      `asip-h2-singbox.service` 跑的就是这份，Clash API 在 9091。
      拿 H2 的 `WARP-H2-*` tag 去主配置 `singbox-config.json` 里翻，
      永远翻不到 —— 实测因此把 56 条正常在跑的 H2 线报成「没有 outbound」。
      那不是配置裂了，是判据从根上问错了问题。
    """
    data, err = _read_h2_singbox()
    if err:
        return set(), err
    outs = data.get("outbounds") or []
    return {str(o.get("tag")) for o in outs
            if isinstance(o, dict) and o.get("tag")}, None


def read_h2_channels(project: Path | None = None) -> tuple[dict[str, dict], str | None]:
    """H2 渲染器自己的台账 `.warp/h2/channels.json` → {member_tag: 渠道记录}。

    WHY 用这份台账而不是自己从 singbox.json 反推（本工具最初 134 条假阳性的根因）：
      H2 一条线涉及**三个不同端口**，很容易认错：
        · `member["port"]`      = 1180+  usque 的 socks 口（daemon 探测用这个）
        · `socks_port`          = 同一件事，台账里的写法
        · `inbound_port`        = 22700+  sing-box 的 direct 入站（sing-box 侧）
      我第一版拿 `member["port"]`(1180+) 去比 `inbounds[].listen_port`(22700+)，
      永远不相等 —— 于是 112 条 H2 线全被报成「没有出口」。
      第二版又拿 `WARP-H2-*` tag 去主配置找 outbound —— H2 根本不在主配置里。

      渲染器已经把「member_tag ↔ inbound_port ↔ unit ↔ device ↔ node」
      写进台账了。**它才是权威**，任何外部核对都该读它，而不是猜命名规则。
    """
    path = ((project or PROJECT) / ".warp" / "h2" / "channels.json")
    if not path.exists():
        return {}, f"没有 H2 台账 {path}（未渲染或未启用 H2）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        return {}, f"读不到 H2 台账 {path}：{e}"
    except ValueError as e:
        return {}, f"H2 台账解析失败 {path}：{e}"
    if not isinstance(data, list):
        return {}, f"H2 台账 {path} 顶层不是数组"
    out: dict[str, dict] = {}
    for c in data:
        if not isinstance(c, dict):
            continue
        tag = c.get("member_tag") or c.get("channel")
        if tag:
            out[str(tag)] = c
    return out, None


def _read_h2_singbox(project: Path | None = None) -> tuple[dict, str | None]:
    """读 `.warp/h2/singbox.json`。读不到返回空 dict + 原因。"""
    path = ((project or PROJECT) / ".warp" / "h2" / "singbox.json")
    if not path.exists():
        return {}, f"没有 H2 配置 {path}（未渲染或未启用 H2）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        return {}, f"读不到 H2 配置 {path}：{e}"
    except ValueError as e:
        return {}, f"H2 配置解析失败 {path}：{e}"
    if not isinstance(data, dict):
        return {}, f"H2 配置 {path} 顶层不是对象"
    return data, None


def read_state(project: Path) -> tuple[dict[str, dict], str | None]:
    """状态台账 `.warp_line_state.json` → {tag: 记录}。"""
    path = project / ".warp_line_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        return {}, f"读不到状态台账 {path}：{e}"
    except ValueError as e:
        return {}, f"状态台账解析失败 {path}：{e}"
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, dict):
        return {}, f"状态台账 {path} 形状异常（缺 lines 字典）"
    return {str(k): v for k, v in lines.items() if isinstance(v, dict)}, None


def read_tier_disabled(declared_wg: set[str], project: Path) -> tuple[set[str], str | None]:
    """当前档位下停用的 WG 线 tag（档位来自 `.warp/tiers.json`）。

    只统计 **members 里声明过的** WG 线：23+ 的闲置补位池与档位是两回事，
    标成「档位停用」是误标。

    展开逻辑复用 `warp_blocks.tier_keep`（一份实现三处调用），但**读文件
    从 `project` 拿** —— `--project` 指向别的数据目录时，档位必须跟着那份
    目录走，否则会出现「台账是 A 的、档位是 B 的」这种混账。
    """
    try:
        data = json.loads((project / ".warp" / "tiers.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError) as e:
        # 档位读不出来 → 按「没有档位 = 全开」处理（与 warp_blocks 一致），
        # 但**必须留痕**：静默当成全开会让 22 条线在无人察觉时全部解禁。
        return set(), f"读不到档位文件 {project / '.warp' / 'tiers.json'}：{e}"
    try:
        cur = str(data.get("current") or "")
        spec = str(((data.get("tiers") or {}).get(cur) or {}).get("wg") or "")
    except (AttributeError, TypeError):
        return set(), f"档位文件形状异常：{project / '.warp' / 'tiers.json'}"
    if not declared_wg:
        return set(), ("members 里没有 WG 线声明（.env 的 WARP_WG_FAMILIES / "
                       "WARP_WG_NODES 没读到），档位按「全开」处理")
    return set(declared_wg) - warp_blocks.tier_keep(spec), None


def read_running_units() -> tuple[list[str], str | None]:
    """在跑的隧道单元 service 名列表。

    自己实现单元扫描（不依赖 daemon 的 `_scan_running_tunnels`，那是 daemon
    的内部函数）：`systemctl list-units --type=service --state=running`。

    tag 的定法照抄 daemon 的既有约定：
      - units 在 members 里声明过的 → 用 members 里的 tag（权威）。
      - `usque-h2-<N>` → `WARP-H2-SCAN-<N>`（探测时编造的假身份）。
      - `warp-wg-<N>`  → `WARP-WG-<N>`。
      - `usque-warp-<N>` → **没有正则兜底**：定不出 tag 就跳过（daemon 同）。
    """
    ok, out = _run(["systemctl", "list-units", "--type=service",
                    "--state=running", "--no-legend", "--no-pager"]
                   + list(TUNNEL_UNIT_GLOBS), timeout=15)
    if not ok:
        return [], "systemctl list-units 失败：查不到在跑的隧道单元"
    services = [ln.split()[0] for ln in out.splitlines() if ln.split()]
    return services, None


def read_unit_files_on_disk() -> tuple[set[str], str | None]:
    """systemd 单元文件**还在磁盘上**的 service 名集合。

    查 `systemctl list-unit-files` 而不是遍历目录：前者是 systemd 自己的
    账本，看到的才是真正会被加载的单元。单元**还在但已停** = 缩容残骸
    （渲染器只增不减），是真问题；单元**不在了** = 只剩状态表里的陈旧记录。
    """
    ok, out = _run(["systemctl", "list-unit-files", "--type=service",
                    "--no-legend", "--no-pager"], timeout=30)
    if not ok:
        return set(), "systemctl list-unit-files 失败：查不到磁盘上的单元文件"
    return {ln.split()[0] for ln in out.splitlines() if ln.split()}, None


def read_h2_used_indexes(project: Path) -> set[str]:
    """当前渲染产物 `.warp/h2/channels.json` 里已占用的 H2 编号。

    用于区分「旧编号体系的线」与「编号被复用的线」：编号是端口契约、不回收，
    所以 `WARP-H2-<COLO>-<N>` 的单元文件在磁盘上可能是**正常的**（当前台账
    里已有同编号的线），而不是缩容残骸。
    """
    try:
        rows = json.loads((project / ".warp" / "h2" / "channels.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(rows, list):
        return set()
    return {str(r.get("index")) for r in rows
            if isinstance(r, dict) and r.get("index") is not None}


# ---------------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------------
def unit_name_for_tag(tag: str, used_h2_idx: set[str]) -> str | None:
    """这条 tag 对应的 systemd 单元文件名；定不出就返回 None。

    `WARP-H2-<COLO>-<N>` 若编号已被当前渲染产物占用 → 返回 None（不是残骸）。

    WHY `WARP-H2-SCAN-<N>` 也要查占用（本工具第二大的假阳性）：
      SCAN-<N> 是「扫描到 usque-h2-<N>.service 在跑、但 members 里没有
      对应声明」时用的兜底 tag。它和正式线**共用同一个编号空间** ——
      `usque-h2-112.service` 既可能是「SCAN 孤儿」，也可能是
      `WARP-H2-NRT-112` 的合法单元。
      我第一版对 SCAN 分支无条件返回单元名，于是渲染完成后新上线的
      112~167 全被判成「缩容遗留」—— 实测 56 条假警报，而它们
      **恰恰是 members 里 112 对 112 完全对齐的正常线**。
      正确判据：编号已被正式成员占用 → 这个 SCAN tag 只是旧台账里的
      陈旧记录，不是遗留单元。
    """
    m = SCAN_TAG_RE.fullmatch(tag)
    if m:
        idx = m.group(1)
        if idx in used_h2_idx:
            return None
        return f"usque-h2-{idx}.service"
    m = H2_COLO_TAG_RE.fullmatch(tag)
    if m:
        idx = m.group(1)
        if idx in used_h2_idx:
            return None
        return f"usque-h2-{idx}.service"
    m = WG_TAG_RE.fullmatch(tag)
    if m:
        return f"warp-wg-{m.group(1)}.service"
    return None


def probe_reason(tag: str, rec: dict | None, in_members: bool,
                 now: float) -> str:
    """这条线「为什么没有可用样本 / 结论」的成因标签。

    口径与 `tools/channel_report.py` 的 `_no_sample_reason` 对齐，保证两处
    说法一致；但**多一档 `端口未开`** —— `last_verdict == "port_closed"`
    说的是「这个端口根本没监听」，跟「隧道活着但取不到出口」是两回事，
    处置动作也不同（前者查 systemd 单元，后者查落地节点）。

    成因按**从具体到笼统**的顺序判定（一个标签只归一类）：
      0. `档位停用` / `补位池闲置` —— last_probe_ts == 0，且**结构上不该探**
         （档位关掉了、或是 WG 补位池）。这两档不是故障，先说清楚免得读报告
         的人把「设计如此」当「漏配」。
      1. `从未探测` / `不在探测名单` —— last_probe_ts == 0。从没探过。
      2. `惩罚期短路` —— 探过，拿到地址发现该地址在惩罚期，在模型层实测之前
         就返回了，没有判据样本。这是正常的换址逻辑，**不是故障**。
      3. `端口未开` —— 探过，端口根本没监听（`last_verdict=port_closed`）。
         daemon 记这个结论时**同时**会打上 `ban_noaddr_since`，所以它必须
         排在 `取不到地址` 前面，否则这个更具体的成因会被笼统的那个吞掉。
      4. `取不到地址` —— 探过，隧道取不到出口地址，没有可入账的结论。
      5. `探测未入账` —— 探过、判了结论，但结论没进 `ban_probes`
         （入账漏记的 bug）。这是**兜底**档：前四档都不成立、且
         `ban_probes` 为空，才算它。
      6. `有样本` —— 台账里有结论也有样本，一切正常。
    """
    rec = rec or {}
    last_probe = float(rec.get("last_probe_ts") or 0)
    if not last_probe:
        # 先判「压根不该探」，再判「该探却没探」。
        # WHY 顺序如此（2026-11-11 实测）：22 条 WG 线档位停用、8 条 WG 补位池，
        # 它们都是**设计上不探**。混进「从未探测」会让这一档看起来像故障，
        # 而真正该问的「名单里该探的线为什么没探」被稀释掉了。
        if rec.get("_tier_off"):
            return "档位停用"
        if rec.get("_spare_pool"):
            return "补位池闲置"
        return "不在探测名单" if not in_members else "从未探测"
    age = _age_str(now - last_probe)
    if rec.get("blocked_notes"):
        return f"惩罚期短路（{age}探过）"
    if rec.get("last_verdict") == "port_closed":
        return f"端口未开（{age}探过）"
    if rec.get("ban_noaddr_since"):
        return f"取不到地址（{age}探过）"
    if rec.get("_tier_off"):
        return f"档位停用（{age}探过，停用前的旧结论）"
    if rec.get("_spare_pool"):
        return f"补位池闲置（{age}探过，旧结论）"
    if not in_members:
        return f"不在探测名单（{age}探过）"
    if not rec.get("ban_probes"):
        return f"探测未入账（{age}探过）"
    return "有样本"


def _age_str(seconds: float) -> str:
    if seconds < 5400:
        return f"{int(seconds / 60)}分钟前"
    if seconds < 172800:
        return f"{int(seconds / 3600)}小时前"
    return f"{int(seconds / 86400)}天前"


def collect(project: Path) -> dict:
    """跑一遍完整对账，返回结构化结果（JSON 模式直接输出它）。"""
    now = time.time()
    warnings: list[str] = []

    members, w = read_members()
    warnings += w
    member_tags = set(members)
    declared_wg = {t for t, m in members.items() if m.get("kind") == "wg"}

    outbounds, out_err = read_outbound_tags(project)
    if out_err:
        warnings.append(out_err)

    state, state_err = read_state(project)
    if state_err:
        warnings.append(state_err)
    state_tags = set(state)

    tier_off, tier_err = read_tier_disabled(declared_wg, project)
    if tier_err:
        warnings.append(tier_err)

    running_services, run_err = read_running_units()
    if run_err:
        warnings.append(run_err)
    on_disk, disk_err = read_unit_files_on_disk()
    if disk_err:
        warnings.append(disk_err)
    used_h2_idx = read_h2_used_indexes(project)

    # --- 单元 → tag（照抄 daemon 的既有约定）-------------------------------
    declared_by_service = {m.get("service"): m for m in members.values()
                           if m.get("service")}
    running_tag_of: dict[str, str] = {}
    undecidable_running: list[str] = []
    for svc in running_services:
        m = declared_by_service.get(svc)
        if m:
            running_tag_of[svc] = str(m.get("tag"))
            continue
        mh = H2_UNIT_RE.fullmatch(svc)
        if mh:
            running_tag_of[svc] = f"WARP-H2-SCAN-{mh.group(1)}"
            continue
        mw = WG_UNIT_RE.fullmatch(svc)
        if mw:
            running_tag_of[svc] = f"WARP-WG-{mw.group(1)}"
            continue
        # `usque-warp-<N>` 没有正则兜底：定不出 tag 就记下来，不猜。
        undecidable_running.append(svc)

    running_tags = set(running_tag_of.values())

    # --- 分类 -------------------------------------------------------------
    # 状态台账里有、探测名单里没有的线
    orphan_state = sorted(state_tags - member_tags)

    # 分类一（问题）：线在跑，却没人探。
    #   判据是**单元在跑**（systemd 事实），不是 tag 前缀 —— 实测 47 条
    #   `WARP-H2-SCAN-*` 残骸对应的单元全是 inactive，按前缀推断会把死线
    #   报成「在跑却没人探」，那是假警报，会盖住真问题。
    scan_unprobed = sorted(t for t in running_tags if t not in member_tags)

    # 分类二（问题）：名单里有，但**它该在的那份** sing-box 配置里没有对应出口。
    #
    # WHY 必须按 kind 分两份配置查（本工具最初 56 条假阳性的根因）：
    #   H2 线不吃主配置 —— 它靠 `.warp/h2/singbox.json` 里的 `h2-sel-<n>`
    #   selector + 22700+ direct 入站，由 `asip-h2-singbox.service` 承载。
    #   CF/WG 线才是主配置 `singbox-config.json` 里的 outbound（tag 同名）。
    #   拿 H2 的 tag 去主配置里翻必然翻不到，于是 56 条正在跑的线被报成
    #   「没有 outbound」—— 假警报，而且会盖住真问题。
    #
    # WHY H2 侧用「端口 -> 入站」而不是「tag -> selector」做判据：
    #   daemon 探测 H2 线只用 `member["port"]`（1180+ 的 socks 口），
    #   从不按 tag 查 outbound。所以「这条线的入站端口在不在」才是它
    #   真实依赖的东西；selector 名（h2-sel-<n>）是渲染器的内部命名，
    #   跟着编号走，拿它当契约反而脆。
    #
    # WHY 读不到配置时必须整类跳过：空集会让**每一条成员**都变成
    # 「没有出口」—— 一堆假警报把真问题埋掉。这份文件由订阅刷新流程
    # 原子替换，读到半截 JSON 是真实可能。
    if out_err:
        missing_out: list[str] = []
    else:
        h2_tags = {t for t, m in members.items() if m.get("kind") == "h2"}
        other_tags = [t for t in member_tags if t not in h2_tags]
        # WHY 排除 tier_off：档位停用的线**故意不跑** —— 单元被 stop、
        # 渲染器也不再为它保 outbound。把它报成「没有出口」是拿正常状态
        # 当故障（实测 22 条 WG 线因此误报）。它们该由 tier_off_with_state
        # 那条规则去管（判据是「还留着结论」），不该在这里重复报。
        bad = [t for t in other_tags if t not in outbounds and t not in tier_off]

        channels, ch_err = read_h2_channels(project)
        if ch_err:
            # 台账读不到时**整类跳过并告警**，绝不把 112 条全报出来。
            warnings.append(f"H2 出口未核对（{ch_err}）")
        else:
            h2_data, h2_err = _read_h2_singbox(project)
            h2_ports = set()
            if not h2_err:
                h2_ports = {int(i["listen_port"]) for i in (h2_data.get("inbounds") or [])
                            if isinstance(i, dict) and i.get("listen_port")}
            for t in sorted(h2_tags):
                if t in tier_off:
                    continue           # 档位停用：同上面 WG 的道理
                c = channels.get(t)
                if not c:
                    bad.append(t)          # 在探测名单里，渲染器却根本不知道这条线
                elif h2_ports and c.get("inbound_port") not in h2_ports:
                    bad.append(t)          # 台账说有入站，sing-box 里却没有
        missing_out = bad

    # 分类三（问题）：档位停用的线，状态台账里却还留着**结论**。
    #   档位停用是对的（省资源），但它意味着这些线**不该有结论**；
    #   有结论说明档位切换时没清干净。
    #
    # WHY 判据是「有结论类字段」而不是「有台账条目」：
    #   台账里**保留条目本身是对的** —— `ban_probes` 是当天的历史样本
    #   （渠道锁判据的原料），档位停用时刻意不清它。所以「档位停用的线在
    #   台账里有条目」是**正常状态**。真正的问题是「还留着结论」——
    #   last_verdict / ip / last_probe_ts 这些描述上一个会话的字段没被
    #   清掉，报表会把旧值显示成当前状态。
    #
    # WHY 台账读不到时整类跳过：state 为空集时「有结论」一条都判不出来，
    # 报成 0 条 = **假阴性**（明明有 47 条残骸，却报「一切正常」）。
    if state_err:
        tier_off_with_state: list[str] = []
    else:
        tier_off_with_state = sorted(
            t for t in member_tags
            if t in tier_off and t in state_tags
            and any((state.get(t) or {}).get(k)
                    for k in ("last_verdict", "ip", "last_probe_ts")))

    # 分类四（非问题）：WG 补位池（tag 是 WARP-WG-23 及以后的）不在名单。
    #   设计上的闲置池，本来就不该被探测 —— 不与真问题混在一起报。
    spare_wg = sorted(t for t in orphan_state
                      if (m := WG_TAG_RE.fullmatch(t)) and int(m.group(1)) >= 23)

    # 档位停用、但台账里**还有条目**（结论已清空）。
    #   这**不是问题**：daemon 停用档位线时刻意保留 `ban_probes`（当天的
    #   历史证据，渠道锁判据的原料），条目留着是对的。列出来是为了让
    #   「档位停用」与「档位切换没清干净」两件事在报告里分得开 ——
    #   前者正常，后者才是要人处理的不一致。
    tier_off_with_entry = sorted(t for t in member_tags
                                 if t in tier_off and t in state_tags)

    # 分类五（问题）：缩容遗留 —— 状态台账里有、名单里没有，且对应 systemd
    #   单元文件**仍在磁盘上**。这是「渲染器只增不减」的直接证据：下一次
    #   缩容会原样重演。
    #   单元文件已不在磁盘 → 只是状态表里的陈旧记录，单列为 orphan_residual
    #   （参考信息，不计入问题）。
    retired: list[str] = []
    orphan_residual: list[str] = []
    for t in orphan_state:
        if t in spare_wg:
            continue
        unit = unit_name_for_tag(t, used_h2_idx)
        if unit and unit in on_disk:
            retired.append(t)
        else:
            orphan_residual.append(t)

    # 探测侧结论：对**每条线**（成员 ∪ 台账里有名有姓的孤儿）算出它属于
    # 哪一类，并附上最后结论、冻结时长、档位状态、单元在不在跑。
    # 这解决「无样本为什么不标原因」。
    #
    # WHY 连台账里不在名单的线也算：只有把它们一起算，「不在探测名单」
    # 这一档才有内容 —— 它正是「文件里有线、daemon 看不见」的直接证据，
    # 而 daemon 自己的报表里这类线永远不会被印出来。
    member_status: list[dict] = []
    for tag in sorted(member_tags | state_tags):
        rec = state.get(tag)
        in_members = tag in member_tags
        verdict = (rec or {}).get("last_verdict") or "-"
        last_probe = float((rec or {}).get("last_probe_ts") or 0)
        frozen_h = round((now - last_probe) / 3600.0, 2) if last_probe else None
        svc = members[tag].get("service") if in_members else None
        unit_running = bool(svc) and svc in running_tag_of
        m_wg = WG_TAG_RE.fullmatch(tag)
        spare = bool(m_wg) and int(m_wg.group(1)) >= 23 and not in_members
        # 传给 probe_reason 的两个结构信号放在 rec 的临时副本里，不改真台账
        hint = dict(rec or {})
        hint["_tier_off"] = tag in tier_off
        hint["_spare_pool"] = spare
        member_status.append({
            "tag": tag,
            "in_members": in_members,
            "reason": probe_reason(tag, hint, in_members, now),
            "last_verdict": verdict,
            "frozen_hours": frozen_h,
            "tier_disabled": tag in tier_off,
            "unit_running": unit_running,
            "service": svc,
            "in_state": tag in state_tags,
            "has_outbound": tag in outbounds,
        })
    member_only = [m for m in member_status if m["in_members"]]

    problems = {
        "scan_unprobed": scan_unprobed,
        "missing_out": missing_out,
        "tier_off_with_state": tier_off_with_state,
        "retired": retired,
    }
    non_problems = {
        "spare_wg": spare_wg,
        "orphan_residual": orphan_residual,
        "tier_off_with_entry": tier_off_with_entry,
    }
    problems_total = sum(len(v) for v in problems.values())

    # 探测侧成因分布（机器可读）：key = 成因类别，value = 完整 tag 列表。
    reason_buckets: dict[str, list[str]] = {}
    for m in member_status:
        reason_buckets.setdefault(_reason_key(m["reason"]), []).append(m["tag"])

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "generated_at_ts": now,
        "project": str(project),
        "read_only": True,
        "sources": {
            "members": len(member_tags),
            "members_wg": len(declared_wg),
            "outbounds": len(outbounds),
            "outbounds_readable": out_err is None,
            "state": len(state_tags),
            "tier_disabled": len(tier_off),
            "running_units": len(running_services),
            "running_units_tagged": len(running_tag_of),
            "unit_files_on_disk": len(on_disk),
        },
        "problems": problems,
        "problems_total": problems_total,
        "non_problems": non_problems,
        "lines": member_status,
        "members": member_only,
        "probe_reasons": reason_buckets,
        "running_tags_not_in_members": sorted(
            t for t in running_tags if t not in member_tags),
        "running_units_without_tag": sorted(undecidable_running),
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------
PROBLEM_BLURB = {
    "scan_unprobed": "扫描到的隧道单元在跑，但 tag 不在探测名单里（线在跑却没人探）",
    "missing_out": "探测名单里有，但 sing-box 配置里没有对应 outbound（换线时 selector 翻不到）",
    "tier_off_with_state": "档位停用的线，状态台账里却还留着结论（档位切换没清干净）",
    "retired": "状态台账里有、探测名单里没有，且 systemd 单元文件仍在磁盘上（缩容遗留，渲染器只增不减）",
}
NONPROBLEM_BLURB = {
    "spare_wg": "WG 补位池（23+）不在探测名单 —— 设计上的闲置池，本来就不该被探测",
    "orphan_residual": "状态表里有、单元已删、也没在任何地方声明的孤立条目 —— 仅作参考",
    "tier_off_with_entry": "档位停用但台账里还留着条目 —— 正常（条目保留当天的 ban_probes 证据），"
                            "结论已清干净就不算不一致",
}
REASON_HINT = {
    "档位停用": "档位（.warp/tiers.json）关掉了这批线 —— 设计上不探，不是故障",
    "补位池闲置": "WG 补位池（23+）不在探测名单 —— 设计上的闲置池，本来就不该被探测",
    "不在探测名单": "线不在 members 里，daemon 永远不会去探它",
    "从未探测": "在名单里但从来没探到过（单元没装/没跑/档位停用）",
    "惩罚期短路": "探过，地址在惩罚期，模型层实测前就返回了 —— 正常逻辑，不是故障",
    "端口未开": "探过但端口根本没监听 —— 查 systemd 单元，不是线路坏了",
    "取不到地址": "探过但隧道取不到出口地址，没有可入账的结论",
    "探测未入账": "探过、判了结论，但结论没进 ban_probes —— 入账漏记的 bug",
    "有样本": "台账里有结论也有样本",
}


def _reason_key(reason: str) -> str:
    """从带后缀的原因标签里取基础类别。"""
    return reason.split("（")[0]


def render_text(rep: dict, max_list: int) -> str:
    out: list[str] = []
    src = rep["sources"]
    out.append("=" * 68)
    out.append(f"跨源巡检报告  {rep['generated_at']}")
    out.append(f"项目：{rep['project']}    （只读巡检，未做任何修改）")
    out.append("=" * 68)
    out.append("")
    out.append("【真相源】")
    out.append(f"  探测名单 members      {src['members']:>4} 条"
               f"（其中 WG 线 {src['members_wg']} 条）")
    out.append(f"  sing-box outbounds    {src['outbounds']:>4} 条"
               f"{'' if src['outbounds_readable'] else '   ⚠ 读不到，已跳过 outbound 对账'}")
    out.append(f"  状态台账 state        {src['state']:>4} 条")
    out.append(f"  在跑的隧道单元        {src['running_units']:>4} 个"
               f"（其中 {src['running_units_tagged']} 个能定出 tag）")
    out.append(f"  磁盘上的单元文件      {src['unit_files_on_disk']:>4} 个")
    out.append(f"  档位停用的线          {src['tier_disabled']:>4} 条")
    out.append("")

    if rep["warnings"]:
        out.append("【警告】")
        for w in rep["warnings"]:
            out.append(f"  ⚠ {w}")
        out.append("")

    out.append("【问题】（需要人来处理的不一致）")
    if rep["problems_total"] == 0:
        out.append("  无。四处真相源对得上。")
    else:
        for key, tags in rep["problems"].items():
            if not tags:
                continue
            out.append(f"  · {key}（{len(tags)} 条）—— {PROBLEM_BLURB[key]}")
            shown = tags[:max_list]
            for t in shown:
                out.append(f"      {t}")
            if len(tags) > len(shown):
                out.append(f"      …… 还有 {len(tags) - len(shown)} 条"
                           f"（--json 看完整列表）")
            out.append("")
        out.append(f"  合计 {rep['problems_total']} 处不一致。")
    out.append("")

    out.append("【非问题】（单独报，不计入上面的不一致）")
    for key, tags in rep["non_problems"].items():
        if not tags:
            continue
        out.append(f"  · {key}（{len(tags)} 条）—— {NONPROBLEM_BLURB[key]}")
        for t in tags[:max_list]:
            out.append(f"      {t}")
        if len(tags) > max_list:
            out.append(f"      …… 还有 {len(tags) - max_list} 条"
                       f"（--json 看完整列表）")
        out.append("")

    if rep["running_units_without_tag"]:
        out.append("【定不出 tag 的在跑单元】（`usque-warp-` 无正则兜底，"
                   "需人工核对）")
        for s in rep["running_units_without_tag"][:max_list]:
            out.append(f"      {s}")
        out.append("")

    # 探测侧结论：按成因分类统计（覆盖成员 ∪ 台账孤儿）
    buckets: dict[str, list[str]] = {}
    for m in rep["lines"]:
        buckets.setdefault(_reason_key(m["reason"]), []).append(m["tag"])
    out.append("【探测侧结论】（每条线属于哪一类）")
    if not buckets:
        out.append("  （无线）")
    for key in ("不在探测名单", "档位停用", "补位池闲置", "从未探测",
                "惩罚期短路", "端口未开", "取不到地址", "探测未入账", "有样本"):
        if key not in buckets:
            continue
        tags = buckets[key]
        out.append(f"  · {key}（{len(tags)} 条）—— {REASON_HINT.get(key, '')}")
        for t in tags[:max_list]:
            out.append(f"      {t}")
        if len(tags) > max_list:
            out.append(f"      …… 还有 {len(tags) - max_list} 条"
                       f"（--json 看完整列表）")
    out.append("")

    # 冻结最久的成员（结论已经不再新鲜）
    frozen = sorted((m for m in rep["lines"] if m["frozen_hours"] is not None),
                    key=lambda m: -m["frozen_hours"])[:max_list]
    if frozen:
        out.append(f"【结论冻结最久的 {len(frozen)} 条线】"
                   "（last_probe_ts 之后一直没更新）")
        for m in frozen:
            flags = []
            if m["tier_disabled"]:
                flags.append("档位停用")
            if not m["in_members"]:
                flags.append("不在探测名单")
            elif not m["unit_running"]:
                flags.append("单元没在跑")
            out.append(f"      {m['tag']:<20} 结论={m['last_verdict']:<12}"
                       f"冻结={m['frozen_hours']}h"
                       f"{'  ' + '、'.join(flags) if flags else ''}")
        out.append("")

    out.append("-" * 68)
    if rep["problems_total"] == 0:
        out.append("结论：五个真相源对得上，没有需要人处理的不一致。")
    else:
        out.append(f"结论：{rep['problems_total']} 处跨源不一致需要人处理 —— "
                   f"其中真问题 {sum(len(v) for v in rep['problems'].values() if v)}"
                   f" 条，遗留/闲置 "
                   f"{sum(len(v) for v in rep['non_problems'].values() if v)}"
                   f" 条（后者不算故障）。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="跨源巡检：members / sing-box outbounds / systemd 单元 / "
                    "状态台账 / 档位 五处对账（只读）")
    ap.add_argument("--json", action="store_true",
                    help="输出机器可读 JSON（每类的完整 tag 列表，不截断）")
    ap.add_argument("--project", default=None,
                    help="项目根目录（默认：脚本所在项目的父目录）")
    ap.add_argument("--max-list", type=int, default=10,
                    help="文本报告里每类列举几个（默认 10）")
    args = ap.parse_args()

    global PROJECT
    if args.project:
        try:
            PROJECT = Path(args.project).resolve()
        except OSError as e:
            print(f"asip_audit: 工具自身出错：--project 路径无法解析：{e}",
                  file=sys.stderr)
            return 2

    try:
        rep = collect(PROJECT)
    except Exception as e:  # noqa: BLE001 — 工具自身出错 → 退出码 2
        print(f"asip_audit: 工具自身出错：{e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render_text(rep, max(0, args.max_list)))

    return 1 if rep["problems_total"] else 0


if __name__ == "__main__":
    sys.exit(main())
