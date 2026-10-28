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
TEST_URL = "https://www.gstatic.com/generate_204"
NODE_TYPES = {"vless", "vmess", "trojan", "anytls", "shadowsocks", "hysteria2"}

OK, DEAD, ERROR = "ok", "dead", "error"
DEMOTE_AFTER = 2       # 连续几轮不通才降级（给抖动一次机会）
POOL_RECHECK_HOURS = 6  # 池内节点多久复测一次（防止"进池即失联"）


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
            return d
    except (OSError, ValueError):
        pass
    return {"nodes": {}}


def save_state(st: dict) -> None:
    """原子写：唯一临时名 + fsync + 0600，崩溃也不会留下空文件。"""
    st["updated"] = time.time()
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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def kill_orphans() -> int:
    """清掉上次异常退出留下的探测进程（只认自己的临时配置路径）。

    注意：服务态下 `PrivateTmp=yes`，每次启动都是全新的私有 /tmp，所以下面扫
    /tmp 文件那半句在服务态是空转（手动跑才有意义）；进程那半句靠的是共享的
    /proc，始终有效。
    """
    r = subprocess.run(["pgrep", "-af", r"sing-box run -c /tmp/dormant-probe-"],
                       capture_output=True, text=True)
    killed = 0
    for line in r.stdout.splitlines():
        pid = line.split()[0]
        try:
            os.kill(int(pid), 15)
            killed += 1
        except (OSError, ValueError):
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
    port = mixed
    used = {clash}
    for inb in cfg.get("inbounds", []):
        while port in used:          # 别和 clash API 或其它入站撞端口
            port += 1
        inb["listen"] = "127.0.0.1"      # 显式环回，缺省是 0.0.0.0
        inb["listen_port"] = port
        used.add(port)
        port += 1
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


def probe_one(tag: str, secret: str, clash: int, mixed: int, timeout: int) -> str:
    _LAST_HTTP.pop(tag, None)      # 提前 return 时别留上一轮的码
    try:
        api(f"/proxies/{selector_tag()}", secret, clash, {"name": tag})
    except Exception:
        return ERROR
    try:
        r = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), "-x", f"http://127.0.0.1:{mixed}", TEST_URL],
            capture_output=True, text=True)
    except Exception:
        return ERROR
    _LAST_HTTP[tag] = (r.stdout or "").strip() or "?"
    if r.stdout.strip() in ("204", "200"):
        return OK
    # 不能用 "000 就算 ERROR"：节点真死时 curl 也返回 000（连不上/超时），
    # 把 000 一律当故障会让"整批垮掉"这件事永远看不见；反过来一律当死，
    # 代理端口没了也会被记成节点死。用本地端口在不在来区分。
    if not port_open(mixed):
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
            print("另一个探测实例正在跑，本次退出（不写状态）")
            os.close(lock_fd)
            return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=7)
    args = ap.parse_args()

    st = load_state()
    all_nodes = args.all
    if not all_nodes and not st["nodes"]:
        # 没有状态文件时，默认模式会算出"没有要测的"而秒退（退出码 0，调度器看不出
        # 区别）。这时自动转全量，否则首次部署等于什么都没做。
        print("状态文件为空 → 自动转全量体检（否则会静默什么都不测）", flush=True)
        all_nodes = True

    tags = targets(all_nodes)
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
        mixed, clash = free_port(), free_port()
        for _tries in range(6):
            if clash != mixed:
                break
            # build_tmp_cfg 的 used={clash} 会把 mixed 入站顶到 mixed+1，而探测仍然
            # curl {mixed}（那是 clash API）→ 整批失败、整池被判死并落盘。撞号概率
            # 约 1/3000，但代价是"整池归零"，必须挡。重抽也限次：真被内核反复
            # 发同一个端口时，宁可这轮跳过，也别无日志地卡到 TimeoutStartSec。
            mixed = free_port()
        else:
            print("✗ 端口分配反复撞号，本轮跳过（不写状态）", flush=True)
            return 5
        cfg_path = build_tmp_cfg(secret, mixed, clash, cfg=cfg_now)
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

    try:
        known = set(st["nodes"])
        for i, tag in enumerate(tags, 1):
            res = probe_one(tag, secret, clash, mixed, args.timeout)
            if res == DEAD and tag not in known:
                # 完全没历史的节点，单次 7s 超时就判死太狠（它可能只是慢）：
                # 加长超时再试一次，还不行才记死。有历史的节点走 strikes。
                res = probe_one(tag, secret, clash, mixed, min(args.timeout * 2, 20))
            if res == OK:
                alive.append(tag)
            elif res == ERROR:
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
    finally:
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
            # 这份临时日志里是 sing-box 的完整输出（含节点参数与临时配置路径）。
            # 尾部已经打进 journal，文件不必留 —— 留着会一次次堆在 /tmp。
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
        # 一条都不通时，最可能是出口/链路整体有问题（账号额度窗、上游被墙），
        # 而不是"这批节点刚好同时全死"。写盘归写盘（strikes 要连续两轮才降级），
        # 但这句必须留痕，否则日志里只有一堆 dead。
        print("⚠ 本轮 0 条通 —— 留意是账号级额度窗还是节点整体失效", flush=True)
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
    save_state(st)
    dead = sum(1 for v in st["nodes"].values() if v.get("alive") is False)
    tail = ("：" + ", ".join(t[:32] for t in stale[:6])) if stale else ""
    print(f"已写 {STATE}（{len(st['nodes'])} 条，判死 {dead} 条，"
          f"清掉陈旧 {len(stale)} 条{tail}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
