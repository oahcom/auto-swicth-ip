#!/usr/bin/env python3
"""沉睡池探测器：把节点逐条实测，写进 .dormant_state.json。

## 为什么要有这个

okz 的订阅是活的、节点全死。把 65 个死节点留在轮换池里，daemon 每轮都在
「撞上死节点 → 标死 30 秒 → 再撞」之间空转（实测 dead=81）。可它们随时可能
复活，直接删掉又等于放弃。

所以分成两件事：
  - **配置文件里永远留着**（出站、tag、全部参数都在）→ 没丢
  - **池子成员按实测动态进出** → 不空转

## 这个脚本踩过、已经堵死的坑（2026-10-28 三方审查，别改回去）

1. **死节点必须能重新被切到。** 线上 `proxy` selector 故意不含沉睡节点，而脚本
   原先整份拷贝线上配置 → PUT 必然失败（`Selector update error: not found`）→
   一律判死 → **池子只会单向缩小，永不复原**。现在临时副本里把 selector 成员
   扩成全部节点。
2. **临时入站必须绑 127.0.0.1。** 原先 `inb.pop("listen")` → sing-box 回落
   `0.0.0.0` → 每 2 小时开一个无鉴权开放代理，Windows 侧 `localhost:17995` 可用。
3. **临时配置必须 0600 且用完即删。** 它是主配置完整拷贝、含全部 password/uuid；
   原先落固定路径 `/tmp/dormant_probe_cfg.json`，umask 022 → 0644，全局可读。
4. **端口必须动态、实例必须有归属。** 固定 17995/19195 + 固定 secret：上一个脚本
   异常退出留下的孤儿占着端口时，新实例 bind 失败却仍能对着**孤儿**的旧快照探测，
   结果打在过期配置上。现在端口动态取、secret 每轮随机（不出示就控不了）。
5. **一次失败不等于死。** 原先单次 curl 超时就判死并落盘。现在连续两轮不过才降级
   （`strikes`），且把三态结论（ok/dead/error）记进状态 —— HTTP 码只打日志，不进状态，事后能分清"真不通"和"探测链路坏了"。
6. **探测链路整体故障时不许写盘。** 三态 OK/DEAD/ERROR，ERROR 超阈值直接放弃本轮。
7. **状态要能自证是同一个节点。** tag 由订阅到达顺序分配（`~N` 后缀），源一换序，
   同一个 tag 可能换指另一个物理节点。每条记录带 `fp` 指纹（server/port/type/ws），
   tag 对不上指纹就整条忽略，不让旧标签的"死"落到新节点头上。
8. **状态里消失的节点要清理**，否则陈旧判死记录会一直生效。

用法：
    /usr/bin/python3 dormant_probe.py             # 只测沉睡的（默认；无状态时自动转全量）
    /usr/bin/python3 dormant_probe.py --all       # 全量体检
    /usr/bin/python3 dormant_probe.py --dry-run   # 只报数，不写状态
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "singbox-config.json"
STATE = ROOT / ".dormant_state.json"
# 临时 sing-box 实例异常退出时留档（旧版无条件删，死因查不到）
INSTANCE_LOG = ".dormant_probe_instance.log"
TEST_URL = "https://www.gstatic.com/generate_204"
NODE_TYPES = {"vless", "vmess", "trojan", "anytls", "shadowsocks", "hysteria2"}

OK, DEAD, ERROR = "ok", "dead", "error"
DEMOTE_AFTER = 2       # 连续几轮不通才降级（给抖动一次机会）
POOL_RECHECK_HOURS = 6  # 池内节点多久复测一次（防止"进池即失联"）
# 首跑全量有 247 条节点（实测），单节点 7s、判死还要 14s 复测，整轮可能
# 超过 daemon 给的子进程预算。被 SIGKILL 时 finally 不跑、状态不落盘，下一轮
# 又从全量开始 —— 永远写不出 .dormant_state.json。所以按预算主动收尾：
# 每 CHECKPOINT_EVERY 条存一次进度，预算用尽就正常退出 RC_MORE（未完待续）。
CHECKPOINT_EVERY = 10
DEFAULT_MAX_SECONDS = 1800
RC_MORE = 8            # 进度已存、还有剩余，调度器必须立即续跑（不等待）


def selector_tag() -> str:
    """池子 selector 的名字。生成侧用 CFG["singbox"]["selector"]，这里跟着它走，
    避免硬编码 "proxy" —— 改名后硬编码会让 pool 变空集、每轮都"测全部"。"""
    try:
        sys.path.insert(0, str(ROOT))
        from config import CFG
        return CFG["singbox"]["selector"]
    except Exception:
        return "proxy"


def node_fp(node: dict) -> str:
    """节点指纹：同 tag 换了物理节点时用来作废旧证据。"""
    ws = ""
    tls = node.get("tls") or {}
    tr = node.get("transport") or {}
    if isinstance(tr, dict):
        ws = f"{tr.get('type', '')}|{tr.get('path', '')}|{tr.get('host', '')}"
    sni = tls.get("server_name") if isinstance(tls, dict) else ""
    return f"{node.get('server')}|{node.get('server_port')}|{node.get('type')}|{ws}|{sni}"


def is_dead_entry(v) -> bool:
    """一条状态记录是否算"判死"。字符串 "false" 也认 —— 与 parse 侧保持一致，
    否则手改一次就让整套机制静默失效。"""
    if not isinstance(v, dict):
        return False
    alive = v.get("alive")
    return alive is False or (
        isinstance(alive, str) and alive.strip().lower() in ("false", "0", "no"))


def load_state() -> dict:
    try:
        with open(STATE) as f:
            d = json.load(f)
        if isinstance(d.get("nodes"), dict):
            d.setdefault("pending", [])
            return d
    except (OSError, ValueError):
        pass
    return {"nodes": {}, "pending": []}


def save_state(st: dict, final: bool = True) -> None:
    """原子写：唯一临时名 + fsync + 0600，崩溃也不会留下空文件。

    `final=False` 是**进度存盘**：`updated` 写 0、`complete` 写 False。
    绝不能把进度存盘写成 updated=now —— daemon 会以为本轮体检完成，把剩下
    两百条压到 6 小时后才测。写 0 的效果是「这轮没跑完，立刻续跑」，
    正好符合主人定标的「探测不等待」。
    """
    st["updated"] = time.time() if final else 0.0
    st["complete"] = bool(final)
    fd, tmp = tempfile.mkstemp(dir=str(ROOT), prefix=".dormant_state.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(st, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE)
        try:                      # 目录也要 fsync，否则掉电后 rename 可能没落盘
            dfd = os.open(str(ROOT), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ephemeral_range() -> tuple[int, int]:
    """内核把出站连接的**源端口**也从这个范围里分。读不到就按 Linux 缺省。"""
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
            lo, hi = (int(x) for x in f.read().split())
        return lo, hi
    except (OSError, ValueError):
        return 32768, 60999


def free_port(exclude=None) -> int:
    """抽一个此刻确实能 bind、且**不在内核临时端口段内**的端口。

    WHY 不能再用 bind(0)（2026-11-11 实测）：
      本机 ip_local_port_range = 43255..47350，只有 4096 个；112 条 WARP 线
      加上大量探测出站把这段占得很满。`bind(0)` 返回的往往是段内的
      "孤立空隙"，而旧 build_tmp_cfg 把第二个入站端口硬推成 `mixed+1`
      —— 实测连抽 20 组，`mixed+1` 有 16 组已被占用（80%）。于是临时
      sing-box 每次都 FATAL 在 mixed-lan，探测永远写不出状态文件。
      换一组端口重试没用：换掉的只有基址，`+1` 照撞。

    避开的第二个好处：出站源端口永远不会落到探测实例监听的端口上，
    真正的 TOCTOU 只剩"另一个进程恰好也挑了这段保留端口"，概率极低，
    main() 里保留的两次重试足够兜住。
    """
    blocked = set(exclude or ())
    lo, hi = _ephemeral_range()
    # 临时段之外的两段保留区；本机的 H2 direct 入站占着 228xx，所以必须
    # 逐个真 bind 校验，不能挑了就用。
    # 审查修正（2026-11-11）：旧版 low 从 20000 起、high 到 60000 止，
    # 内核 lo<20000 时 low 为空、hi>=60000 时 high 为空 —— 两段同时为空
    # 就退回 bind(0)，正好回到这次要修掉的老缺陷。两端放宽到协议合法边界，
    # 反正每个候选都会真 bind 校验。
    low = range(1024, min(lo, 32000))
    high = range(max(hi + 1, 48000), 65535)
    candidates = list(low) + list(high)
    random.shuffle(candidates)
    # 上限只防极端占满时遍历上万个端口；正常第一个候选就返回。
    for port in candidates[:4096]:
        if port in blocked:
            continue
        sk = socket.socket()
        try:
            sk.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sk.close()
    # 保留段全被占（异常）才退回内核分配；仍做排除检查，最多试 256 次。
    for _ in range(256):
        sk = socket.socket()
        try:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        except OSError:
            continue
        finally:
            sk.close()
        if port not in blocked:
            return port
    raise RuntimeError("找不到空闲端口（保留段与内核临时段都被占满）")


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_ppid(pid: int) -> int:
    """/proc/<pid>/stat: pid (comm) state ppid ... —— comm 可能含空格/括号。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return int(f.read().split(") ", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return -1


def _active_probe_pids() -> set[int]:
    """当前活着的 dormant_probe.py 进程（排除自己）。"""
    me = os.getpid()
    out: set[int] = set()
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if n.isdigit() and int(n) != me and "dormant_probe.py" in _proc_cmdline(int(n)):
            out.add(int(n))
    return out


def kill_orphans() -> int:
    """只清**没有活跃主人**的临时 sing-box，绝不碰仍在服务的实例。

    WHY（2026-11-11 实测事故）：旧版无差别 `pgrep -af` + SIGTERM 杀掉所有
    `sing-box run -c /tmp/dormant-probe-*` 进程。`PrivateTmp` 只隔离 /tmp
    **文件**，不隔离 /proc：服务态与手动跑的实例互相看得见。服务重启时
    `KillMode=process` 只杀主进程，上一轮体检子进程连同它的临时 sing-box
    会残留；新实例一旦启动就把它杀掉 → 前一个剩下的节点全部 curl 失败，
    被记成 error。实例早期被杀就是 225/247 全 error、整轮作废，状态永远
    写不出来；跑到后段被杀就是中途 `port-closed` 后 19 条 error。

    判据：`start_new_session=True` 启的 sing-box，其 PPID 就是 spawn 它的
    dormant_probe.py。PPID 在活跃集合里 → 有主，跳过；PPID=1（父进程被
    KillMode=process 干掉）或已消失 → 才是真孤儿，可以清。
    """
    r = subprocess.run(["pgrep", "-af", r"sing-box run -c /tmp/dormant-probe-"],
                       capture_output=True, text=True)
    active = _active_probe_pids()
    killed = 0
    for line in r.stdout.splitlines():
        try:
            pid = int(line.split()[0])
        except (ValueError, IndexError):
            continue
        if _proc_ppid(pid) in active:
            continue                      # 有主：另一个实例正在用它
        try:
            os.kill(pid, 15)
            killed += 1
        except OSError:
            pass
    for d in {Path(tempfile.gettempdir()), ROOT}:
        # .log 也要清：Popen 抛异常或启动期被 SIGTERM 时，那份 0600 的 sing-box
        # 输出没人删，手工跑几次就会在 /tmp 里堆一串。
        for p in list(d.glob("dormant-probe-*.json")) + list(d.glob("dormant-probe-*.log")):
            try:
                p.unlink()
            except OSError:
                pass
    return killed


def targets(all_nodes: bool) -> list[str]:
    cfg = json.load(open(LIVE))
    sel = selector_tag()
    pool: set[str] = set()
    for o in cfg["outbounds"]:
        if o.get("type") == "selector" and o.get("tag") == sel:
            pool = set(o["outbounds"])
    tags = [o["tag"] for o in cfg["outbounds"] if o.get("type") in NODE_TYPES]
    if all_nodes:
        return tags
    st = load_state()
    now = time.time()
    out: list[str] = []
    for t in tags:
        v = st["nodes"].get(t)
        if t not in pool or is_dead_entry(v):
            # 池外的、已经判死的：每轮都测（这才是"等它复活"的部分）
            out.append(t)
            continue
        # 池内的也要复测，否则「活着进池 → 之后死了 → 永远不测 → state 永远
        # alive:true → 死节点一直留在池子里」，机制会静默退化回原样。
        try:
            checked = float((v or {}).get("checked") or 0)
        except (TypeError, ValueError):
            checked = 0.0
        if not checked or (now - checked) > POOL_RECHECK_HOURS * 3600:
            out.append(t)
    return out


def build_tmp_cfg(secret: str, mixed: int, clash: int, cfg=None) -> str:
    cfg = cfg if cfg is not None else json.load(open(LIVE))
    used = {mixed, clash}
    for idx, inb in enumerate(cfg.get("inbounds", [])):
        if idx == 0:
            port = mixed                 # curl 用的代理端口，由 main 传入
        else:
            # 每个入站各自抽一个**实测空闲**的端口。旧代码是 `port += 1`
            # 硬推，而基址是临时段里的孤立空隙，+1 实测 80% 已被出站连接
            # 占用 → 临时实例次次 FATAL（详见 free_port 的注释）。
            port = free_port(used)
        inb["listen"] = "127.0.0.1"      # 显式环回，缺省是 0.0.0.0
        inb["listen_port"] = port
        used.add(port)
    cfg["experimental"]["clash_api"].update(
        {"external_controller": f"127.0.0.1:{clash}", "secret": secret})

    # 临时副本里的池子 selector 必须包含**全部**节点：线上的池子故意不含沉睡节点，
    # 照抄就切不到要测的对象，全判 ERROR（这就是"池子只会单向缩小"的成因）。
    node_tags = {o["tag"] for o in cfg["outbounds"] if o.get("type") in NODE_TYPES}
    for o in cfg["outbounds"]:
        if o.get("type") == "selector" and (set(o.get("outbounds") or []) & node_tags):
            o["outbounds"] = sorted(node_tags) + ["direct"]
            o["default"] = o["outbounds"][0]

    fd, path = tempfile.mkstemp(prefix="dormant-probe-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
    except BaseException:
        os.unlink(path)
        raise
    return path


def api(path: str, secret: str, clash: int, data=None):
    url = f"http://127.0.0.1:{clash}{path}"
    h = {"Authorization": f"Bearer {secret}"}
    if data is None:
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=h), timeout=10).read())
    urllib.request.urlopen(urllib.request.Request(
        url, data=json.dumps(data).encode(), headers=h, method="PUT"),
        timeout=10).read()


def port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


_LAST_HTTP: dict = {}      # tag → 这次探到的 HTTP 码（或 "port-closed"）
_LAST_ERROR: str = ""      # 最近一次 ERROR 的具体原因（首条故障会打进日志）


def probe_one(tag: str, secret: str, clash: int, mixed: int, timeout: int) -> str:
    global _LAST_ERROR
    _LAST_HTTP.pop(tag, None)      # 提前 return 时别留上一轮的码
    try:
        api(f"/proxies/{selector_tag()}", secret, clash, {"name": tag})
    except Exception as e:
        _LAST_ERROR = f"clash API 切换失败：{type(e).__name__}: {e}"
        return ERROR
    try:
        r = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), "-x", f"http://127.0.0.1:{mixed}", TEST_URL],
            capture_output=True, text=True)
    except Exception as e:
        _LAST_ERROR = f"curl 执行失败：{type(e).__name__}: {e}"
        return ERROR
    _LAST_HTTP[tag] = (r.stdout or "").strip() or "?"
    if r.stdout.strip() in ("204", "200"):
        return OK
    # 不能用 "000 就算 ERROR"：节点真死时 curl 也返回 000（连不上/超时），
    # 把 000 一律当故障会让"整批垮掉"这件事永远看不见；反过来一律当死，
    # 代理端口没了也会被记成节点死。用本地端口在不在来区分。
    if not port_open(mixed):
        _LAST_ERROR = (f"临时代理端口 {mixed} 已关闭"
                       "（临时 sing-box 实例可能中途崩了）")
        _LAST_HTTP[tag] = "port-closed"
        return ERROR
    return DEAD


# 锁必须放在**两个实例都看得到**的地方。放 /tmp 是错的：服务态 `PrivateTmp=yes`
# 会把 /tmp 换成私有 tmpfs，锁文件落在里面永远没有第二个进程来竞争 —— 定时器触发
# 的探测和运维手动跑依然并发，两边各起临时 sing-box、各自 PUT selector、最后写状态，
# 谁后写谁赢（2026-10-28 三轮审查抓出来的）。
LOCK_PATH = str(ROOT / ".dormant_probe.lock")


def main() -> int:
    # 并发保护：定时器（RandomizedDelaySec 宽 5 分钟）和手动跑很容易重叠。
    # 没有锁时，后启动的那个会 kill_orphans 掉前一个的 sing-box，前一个剩下的
    # 节点全部 curl 失败 → 被记成"死"并落盘。宁可这次不跑。
    import fcntl
    lock_fd = None
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    except PermissionError as e:
        # 典型场景：有人用 root 手动跑过一次，锁文件变成 root:root 0600，之后
        # administrator 的定时器就打不开了。这里不能崩、也不能静默 —— 喊出来，
        # 并且**跳过加锁继续跑**（宁可少一层保护，也比整轮探测不跑强）。
        healed = False
        try:
            os.unlink(LOCK_PATH)
            lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
            healed = True
        except OSError:
            pass
        if healed:
            print(f"⚠ 锁文件属主/权限不对（{e}），已重建", flush=True)
        else:
            msg = (f"⚠ 锁文件打不开（{e}）；本次**不加锁**继续跑，"
                   f"可能与另一实例并发。修法：sudo chown administrator {LOCK_PATH}")
            print(msg, flush=True)
            try:
                with open(ROOT / "daemon.log", "a") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [沉睡池] {msg}\n")
            except OSError:
                pass
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 必须返回非 0：调度器按退出码决定"6h 后再来"还是"30 分钟后重试"。
            # 以前这里 return 0，等于把"并发跳过"伪装成体检成功。
            print("另一个探测实例正在跑，本次退出（不写状态）")
            os.close(lock_fd)
            return 7

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=7)
    ap.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS,
                    help="本轮体检的时间预算；用尽就存进度并退出 RC_MORE，绝不硬扛到被 kill")
    args = ap.parse_args()

    st = load_state()
    all_nodes = args.all
    if not all_nodes and not st["nodes"]:
        # 没有状态文件时，默认模式会算出"没有要测的"而秒退（退出码 0，调度器看不出
        # 区别）。这时自动转全量，否则首次部署等于什么都没做。
        print("状态文件为空 → 自动转全量体检（否则会静默什么都不测）", flush=True)
        all_nodes = True

    resumed = [t for t in st.get("pending", []) if isinstance(t, str) and t]
    if resumed and not args.all:
        # 上一轮预算用尽：只测剩下的，不重新全量。断点续跑靠落盘的 pending，
        # 不能靠 targets() —— 它对「池外节点」每轮都返回，永远不会收敛。
        tags = resumed
        print(f"续跑上次未完成的体检：剩 {len(tags)} 条", flush=True)
    else:
        tags = targets(all_nodes)
        if not args.dry_run:
            st["pending"] = list(tags)
    if args.limit:
        tags = tags[: args.limit]
    if not tags:
        print("没有要测的节点")
        return 0

    orphans = kill_orphans()
    if orphans:
        print(f"清掉了 {orphans} 个上次遗留的探测进程")

    # 先读一次配置：指纹和临时副本基于同一份快照，避免中途 daemon 换配置导致
    # "按旧配置探测、按新指纹记录"的错配。
    try:
        cfg_now = json.load(open(LIVE))
    except Exception as e:
        print(f"✗ 读不到现役配置 {LIVE}（{e}），本轮不探测、不写状态", flush=True)
        return 2
    fps = {o["tag"]: node_fp(o) for o in cfg_now["outbounds"]
           if o.get("type") in NODE_TYPES}

    print(f"待测 {len(tags)} 条（{'全量' if all_nodes else '仅沉睡'}）…", flush=True)
    proc = None
    errf = None
    secret = mixed = clash = cfg_path = None
    last_log = ""
    # 最多试两次。第一次起不来最常见的原因是 free_port() 交出去的端口在 sing-box
    # 绑上之前被内核当成了出站源端口（本机 12 条 WARP 线在跑，出站连接很多），
    # 换一组端口通常就好了。
    for attempt in (1, 2):
        secret = secrets.token_urlsafe(18)
        # clash API 绝不能和 mixed 同号：探测 curl {mixed} 会打到 clash API，
        # 整批失败并把整池判死落盘。现在由 exclude 直接保证，不再靠重抽。
        mixed = free_port()
        clash = free_port({mixed})
        # 最多试两次。剩下的偶发风险只有 TOCTOU（端口在 sing-box bind 之前
        # 被别的进程抢走），换一组端口通常就好。
        try:
            cfg_path = build_tmp_cfg(secret, mixed, clash, cfg=cfg_now)
        except RuntimeError as e:
            print(f"✗ 临时实例端口分配失败：{e}，本轮跳过（不写状态）", flush=True)
            return 5
        errf = open(os.path.join(tempfile.gettempdir(),
                                 f"dormant-probe-{os.getpid()}-{attempt}.log"), "w+")
        os.chmod(errf.name, 0o600)
        try:
            proc = subprocess.Popen(
                [str(ROOT / "sing-box"), "run", "-c", cfg_path],
                stdout=errf, stderr=errf, start_new_session=True)
        except Exception as e:
            # sing-box 不存在/没执行权时 Popen 直接抛。此时 finally 还没生效
            # （try 在后面才开始），临时配置和这份 0600 日志都会留在盘上。
            try:
                errf.close()
                os.unlink(errf.name)
            except OSError:
                pass
            try:
                os.unlink(cfg_path)
            except OSError:
                pass
            print(f"✗ sing-box 起不来：{e}（{ROOT / 'sing-box'}）", flush=True)
            return 4
        ok = False
        for _ in range(40):
            try:
                api("/proxies", secret, clash)
                ok = True
                break
            except Exception:
                if proc.poll() is not None:      # 进程已经退了，别傻等满 20 秒
                    break
                time.sleep(0.5)
        if ok:
            print(f"  临时实例 127.0.0.1:{mixed} / api {clash}", flush=True)
            break
        # 失败时把 sing-box 自己说的话打出来 —— "端口被占 / 配置 FATAL / 二进制没了"
        # 以前都是一句"起不来"，事后查不动（19:04 那次就是）。
        try:
            errf.flush()
            errf.seek(0)
            last_log = errf.read().strip()[-500:]
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc = None
        try:
            os.unlink(cfg_path)
        except OSError:
            pass
        if attempt == 1:
            print(f"⚠ 临时实例第 1 次起不来（换一组端口重试）: "
                  f"{last_log or '（无输出）'}", flush=True)
    if proc is None:
        print(f"✗ 临时实例起不来，不写任何状态（端口 {mixed}/{clash}）")
        print(f"   sing-box 输出尾部：{last_log or '（空）'}")
        return 1

    # 这三个在上一版补丁里被连带删掉了（替换区间跨过了它们的声明）。
    alive: list[str] = []
    errors = 0
    transitions: list[tuple[str, bool, bool]] = []   # (tag, 之前, 现在)
    started = time.time()

    def _mark_done(t: str) -> None:
        st["pending"] = [x for x in st.get("pending", []) if x != t]

    try:
        known = set(st["nodes"])
        for i, tag in enumerate(tags, 1):
            if tag not in fps:
                # 续跑期间订阅换过、这个 tag 已不在现役配置里：直接销账，
                # 别让幽灵 tag 计入 errors 把整轮误判成「探测链路坏」。
                _mark_done(tag)
                continue
            res = probe_one(tag, secret, clash, mixed, args.timeout)
            if res == DEAD and tag not in known:
                # 完全没历史的节点，单次 7s 超时就判死太狠（它可能只是慢）：
                # 加长超时再试一次，还不行才记死。有历史的节点走 strikes。
                res = probe_one(tag, secret, clash, mixed, min(args.timeout * 2, 20))
            instance_dead = (proc is not None and proc.poll() is not None)
            if res == ERROR and (instance_dead
                                 or _LAST_HTTP.get(tag) == "port-closed"):
                # 临时实例已经退出：后面每一条都只会是 error（clash API 连不上、
                # curl 也走不通），继续跑只是把整轮污染成「探测链路坏」，
                # 于是 errors > 阈值 → 整轮作废、状态永远写不出来。
                # 正确做法：保住已测完的部分，把没测的留给下一轮立即续跑。
                try:
                    errf.flush()
                    errf.seek(0)
                    tail = (errf.read() or "").strip()[-600:]
                except Exception:
                    tail = ""
                print(f"✗ 临时实例在第 {i}/{len(tags)} 条时退出"
                      f"（rc={getattr(proc, 'returncode', None)}）；本轮到此为止",
                      flush=True)
                if tail:
                    print("  实例日志尾部：\n" + tail, flush=True)
                if i <= 1:
                    # 一条都没测成就崩：没有可保留的进度，如实报故障，
                    # 别用 RC_MORE 制造「立即续跑但每轮都原地崩」的空转。
                    return 3
                if not args.dry_run:
                    # 本条没测完，不能 _mark_done；pending 里自然还留着它和后面所有条。
                    save_state(st, final=False)
                    print(f"  已存进度，剩 {len(st.get('pending') or [])} 条立即续跑",
                          flush=True)
                return RC_MORE
            if res == OK:
                alive.append(tag)
            elif res == ERROR:
                if errors == 0:
                    # 首条故障的原因最值钱：225/247 全 error 时，光看
                    # "探测故障" 分不清是实例崩了、API 挂了还是端口没了。
                    print(f"  ⚠ 首条探测故障原因：{_LAST_ERROR or '未知'}",
                          flush=True)
                errors += 1
            code = _LAST_HTTP.get(tag, "")
            print(f"  [{i}/{len(tags)}] {res:<5} {tag[:48]}"
                  f"{('  http=' + code) if code else ''}", flush=True)
            if res == ERROR:
                # 探测链路坏了 ≠ 节点死了。以前这里直接 continue，状态里只剩
                # ok/dead 两种，事后分不清"节点不通"和"我这套探测坏了"。
                # 只给已有记录的节点标 result=error，不动 alive/strikes；
                # 没记录的节点不凭空造一条（那一轮它根本没被测到）。
                old_e = st["nodes"].get(tag)
                if isinstance(old_e, dict):
                    old_e["result"] = "error"
                _mark_done(tag)
                continue
            old = st["nodes"].get(tag)
            # tag 是订阅到达顺序分配的，可能换指另一个物理节点。指纹不符就把旧
            # 记录当没有 —— 否则新节点会继承旧节点的 strikes，一次没试就判死。
            if (isinstance(old, dict) and old.get("fp") and fps.get(tag)
                    and old["fp"] != fps[tag]):
                old = None
            strikes = 0 if res == OK else int((old or {}).get("strikes") or 0) + 1
            if res == OK:
                alive_now = True
            elif strikes >= DEMOTE_AFTER:
                alive_now = False
            else:
                # 第一次不通只记一笔，不降级：给 7s 超时和 TLS 抖动一次机会
                alive_now = bool((old or {}).get("alive"))
            prev_alive = old.get("alive") if isinstance(old, dict) else None
            if isinstance(prev_alive, bool) and prev_alive != alive_now:
                transitions.append((tag, prev_alive, alive_now))
            st["nodes"][tag] = {"alive": alive_now, "checked": time.time(),
                                "reason": "probe", "strikes": strikes,
                                "result": res, "fp": fps.get(tag, "")}
            _mark_done(tag)
            truncated = (args.max_seconds > 0 and
                         time.time() - started >= args.max_seconds)
            if not args.dry_run and (i % CHECKPOINT_EVERY == 0 or truncated):
                save_state(st, final=False)
            if truncated:
                # 正常 return（不是被 SIGKILL）：外层 finally 会清掉临时实例，
                # 状态已落盘且 updated=0 → daemon 下一 tick 立刻续跑剩下的。
                print(f"⏱ 本轮体检预算 {args.max_seconds}s 用尽：进度已存，"
                      f"剩 {len(st.get('pending') or [])} 条立即续跑（不等待）",
                      flush=True)
                return RC_MORE
    finally:
        # 必须在 terminate() 之前取：之后 poll() 恒非 None，就分不清
        # 「实例自己崩了」和「本轮正常收尾」了。
        crashed_before_cleanup = bool(proc is not None and proc.poll() is not None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            os.unlink(cfg_path)
        except OSError:
            pass
        if errf is not None:
            # 旧版无条件 unlink，只在「启动失败」时把尾部打进 stdout；
            # 实例**跑到一半崩了**时 sing-box 自己说的话全被删光，
            # 225/247 全 error 查了三轮都拿不到死因。异常轮必须留档。
            abnormal = crashed_before_cleanup or errors > 0
            try:
                errf.flush()
                errf.seek(0)
                content = errf.read()
            except Exception:
                content = ""
            if abnormal and content.strip():
                try:
                    with open(os.path.join(str(ROOT), INSTANCE_LOG),
                              "w", encoding="utf-8") as f:
                        f.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} "
                                f"rc={getattr(proc, 'returncode', None)} "
                                f"errors={errors}\n")
                        f.write(content[-20000:])
                except OSError:
                    pass
            try:
                errf.close()
            except Exception:
                pass
            try:
                os.unlink(errf.name)
            except OSError:
                pass

    print(f"\n结果：通 {len(alive)} / {len(tags)}，探测故障 {errors} 条")
    if not alive and len(tags) >= 10:
        # 一条都不通时，最可能是出口/链路整体有问题（上游被墙、整池出口不可用），
        # 而不是"这批节点刚好同时全死"。写盘归写盘（strikes 要连续两轮才降级），
        # 但这句必须留痕，否则日志里只有一堆 dead。
        print("⚠ 本轮 0 条通 —— 优先核查出口/链路整体状态", flush=True)
    # 进出池的迁移单独打一行：原来只能靠逐条 ok/dead 反推谁进谁出
    up = [t for t, _o, n in transitions if n]
    down = [t for t, _o, n in transitions if not n]
    if up:
        print("↑ 回池 " + str(len(up)) + " 条: " + ", ".join(t[:40] for t in up[:8]))
    if down:
        print("↓ 出池 " + str(len(down)) + " 条: " + ", ".join(t[:40] for t in down[:8]))
    limit = max(3, len(tags) // 3)
    # 小批量（沉睡池按 6h 分批，2~3 条很常见）全 error 时，errors > limit 不成立
    # → 以前会 exit 0、状态照写、日志一个字都没有：链路坏了和"这几条都不通"
    # 在小批量下完全同形。全垮就必须算故障，跟批量大小无关。
    all_broken = bool(tags) and errors == len(tags)
    # dry-run 只是"不写盘"，不是"不报错"：运维查链路最常用的就是
    # `--dry-run --limit 2`，全坏还返回 0 等于把最需要看见的事藏起来。
    if all_broken:
        print(f"✗ 本轮 {errors}/{len(tags)} 条全部探测故障（不是节点死，是探测链路坏），"
              f"不写状态", file=sys.stderr)
        return 3
    if errors > limit:
        print(f"✗ 探测故障 {errors} 条（阈值 {limit}），放弃本轮，不写状态",
              file=sys.stderr)
        # 退出码分家：2 = 配置读不到，3 = 探测链路故障过多。
        # 两个都用 2 的话，ExecStopPost 那句"探测失败 exit=2"看不出是哪种，
        # 而这两种的修法完全不同（一个是修配置，一个是修链路）。
        return 3

    # 清理：配置里已经没有的 tag 一律删掉，免得陈旧判死记录天长日久
    live_tags = set(fps)
    stale = [t for t in st["nodes"] if t not in live_tags]
    for t in stale:
        del st["nodes"][t]

    if args.dry_run:
        print("（--dry-run，未写状态）")
        return 0
    st["pending"] = []
    save_state(st)
    dead = sum(1 for v in st["nodes"].values() if v.get("alive") is False)
    tail = ("：" + ", ".join(t[:32] for t in stale[:6])) if stale else ""
    print(f"已写 {STATE}（{len(st['nodes'])} 条，判死 {dead} 条，"
          f"清掉陈旧 {len(stale)} 条{tail}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
