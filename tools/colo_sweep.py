#!/usr/bin/env python3
"""MASQUE 承载能力测量：按「成功率」而非「能不能」评判。

为什么不用二元判定
------------------
同一节点 + 同一设备，间隔几分钟测两次会一次成功一次超时（hk-aws-122 实测）。
中继对 MASQUE 的 UDP 转发是间歇性的，单次成败同时混进了：
握手时机、中继瞬时限速、CF 侧抖动。所以「失败一次就判死」会系统性低估。

测量方式
--------
每个节点建一次完整链路（节点 socks → masque_bridge → usque socks），
然后**轮询** trace：每 3s 打一次，最多 45s。成功立刻记 colo 并结束，
失败才等满。比固定的 sleep(15) 更准也更快。

用法
----
    sudo -u administrator python3 tools/colo_sweep.py --attempts 2
    sudo -u administrator python3 tools/colo_sweep.py --tags-file /tmp/a_tags.txt
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DIR))

USQUE = str(DIR / ".warp/usque3/usque")
BRIDGE = str(DIR / "tools/masque_bridge.py")
SINGBOX = str(DIR / "sing-box")
DST = "162.159.198.2:443"
LOCK = threading.Lock()


def node_cfg(node: dict, port: int) -> dict:
    from tools.screen_udp_matrix import node_cfg as _cfg
    return _cfg(node, port)


def trace(port: int, timeout: int = 6) -> dict:
    r = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "--noproxy", "",
         "--proxy", f"socks5h://127.0.0.1:{port}",
         "https://www.cloudflare.com/cdn-cgi/trace"],
        capture_output=True, text=True)
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


def measure(tag: str, node: dict, device: int, slot: int,
            poll_s: int = 40, step: int = 3) -> dict:
    """建链 + 轮询，返回 {ok, colo, ip, secs}。"""
    node_port = 21000 + slot * 10
    bridge_port = 45000 + slot * 10
    out_port = 12000 + slot * 10
    rec = {"tag": tag, "device": f"usque{device}", "ok": False}
    tf = Path(tempfile.mkdtemp(prefix=f"cs{slot}_")) / "cfg.json"
    tf.write_text(json.dumps(node_cfg(node, node_port)))
    procs = []
    try:
        procs.append(subprocess.Popen([SINGBOX, "run", "-c", str(tf)],
                                       stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL,
                                       start_new_session=True))
        time.sleep(2.5)
        procs.append(subprocess.Popen(
            [sys.executable, BRIDGE, "--node-socks", str(node_port),
             "--listen", str(bridge_port), "--dst", DST],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
        time.sleep(1.5)

        dev_cfg = DIR / f".warp/usque{device}" / "config.json"
        cfg = json.load(open(dev_cfg))
        cfg["endpoint_v4"] = "127.0.0.2"
        json.dump(cfg, open(dev_cfg, "w"))
        procs.append(subprocess.Popen(
            [USQUE, "socks", "-c", str(dev_cfg), "-P", str(bridge_port),
             "-b", "127.0.0.1", "-p", str(out_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))

        # 单发长超时 curl，最多 3 轮。理由：
        # 1) 短超时轮询会 abort 掉正在握手的 QUIC（实测 3s/8s 轮询 0/4）；
        # 2) usque 首次 dial 常失败（"timeout: no recent network activity"），
        #    之后回到 idle 等**新的**出站活动才重连 —— 所以每轮必须是一次
        #    新的 curl 请求，而不是同一个请求继续等。
        t0 = time.time()
        for _round in range(3):
            t = trace(out_port, timeout=poll_s)
            if t.get("warp") == "on":
                rec.update(ok=True, colo=t.get("colo"), loc=t.get("loc"),
                           ip=t.get("ip"), secs=round(time.time() - t0, 1),
                           rounds=_round + 1)
                break
            time.sleep(1)
    except Exception as e:  # 单点异常不能中断整批
        rec["err"] = repr(e)
    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), 15)
            except OSError:
                try:
                    p.terminate()
                except OSError:
                    pass
        time.sleep(0.5)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default=str(DIR / "dormant-outbounds.json"))
    ap.add_argument("--tags-file", default="")
    ap.add_argument("--tags", nargs="*", default=[])
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--poll", type=int, default=40)
    ap.add_argument("--device-base", type=int, default=12)
    ap.add_argument("--out", default="/tmp/opencode/colo_sweep.json")
    a = ap.parse_args()

    nodes = {x["tag"]: x for x in json.loads(Path(a.inputs).read_text())}
    tags = list(a.tags)
    if a.tags_file:
        tags += [l.strip() for l in Path(a.tags_file).read_text().splitlines() if l.strip()]
    tags = [t for t in tags if t in nodes]
    print(f"节点 {len(tags)} × {a.attempts} 次，并发 {a.workers}\n")

    results: list[dict] = []
    lock = threading.Lock()

    def work(slot: int, tag: str, attempt: int) -> None:
        dev = a.device_base + (slot + attempt) % 9
        r = measure(tag, nodes[tag], dev, slot, poll_s=a.poll)
        r["attempt"] = attempt + 1
        with lock:
            results.append(r)
            ok = "✓" if r["ok"] else "✗"
            print(f"  {ok} {tag[:42]:42s} try{r['attempt']} "
                  f"{r.get('colo','-'):>4s} {r.get('secs','-'):>5} "
                  f"{r.get('device','')}", flush=True)

    def worker(slot: int, my_tags: list[str]) -> None:
        # 一个 worker 一个线程、串行跑自己的 tag 队列。之前按 tag 起线程但
        # slot 仍取 worker 序号，同 slot 的线程共用 21000/45000/12000 这套
        # 端口，互相抢 bind → 整批 0/29（2 个 tag 时每 worker 只有一个线程，
        # 所以没暴露）。
        for tag in my_tags:
            for attempt in range(a.attempts):
                work(slot, tag, attempt)
                time.sleep(1)

    threads = [threading.Thread(target=worker, args=(w, tags[w::a.workers]))
               for w in range(a.workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(results, indent=1, ensure_ascii=False))

    print("\n=== 按节点汇总 ===")
    agg: dict[str, dict] = {}
    for r in results:
        s = agg.setdefault(r["tag"], {"ok": 0, "n": 0, "colos": set()})
        s["n"] += 1
        if r["ok"]:
            s["ok"] += 1
            s["colos"].add(r.get("colo") or "?")
    rows = sorted(agg.items(), key=lambda kv: -kv[1]["ok"])
    for tag, s in rows:
        print(f"  {s['ok']}/{s['n']}  {tag[:44]:44s} {','.join(sorted(s['colos'])) or '-'}")
    alive = [t for t, s in agg.items() if s["ok"]]
    print(f"\n至少成功一次: {len(alive)}/{len(agg)}  → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
