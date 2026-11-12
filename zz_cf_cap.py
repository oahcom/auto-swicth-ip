# -*- coding: utf-8 -*-
"""测 CF 池子的单 IP 容量：v4-h2 入口 vs v6-h2 入口各起一条**全新注册**的设备。

为什么必须用新设备：生产那 9 条的出口 IP 今天已经被打空（判活返回 quota，
Retry-After 8h+），拿它们测容量只会得到 0。注册新设备 → WARP 分配新出口 IP
→ 从零额度开始打，才能测出真实上限。

会话：每次 probe 内部用 `_pick_session()` 从当天 15 个里随机取，池子上限 15 个，
不会撞到 L1 的 40 个不同 session 闸；所以这里打到的上限反映的是 L2 额度。

用法: python3 zz_cf_cap.py v4h2 [maxtotal] [timeout]
      python3 zz_cf_cap.py v6h2
"""
import json
import os
import pathlib
import subprocess
import sys
import time

import probe_warp as P

BASE = pathlib.Path("/home/administrator/dkk-projects/auto-switch-ip/.warp")

PLAN = {
    "v4h2": {"dir": "_capv4", "port": 11501, "key": "endpoint_h2_v4",
             "endpoint": "162.159.198.3", "family": "v4", "name": "cf-cap-v4"},
    "v6h2": {"dir": "_capv6", "port": 11502, "key": "endpoint_h2_v6",
             "endpoint": "2606:4700:103::2", "family": "v6", "name": "cf-cap-v6"},
}


def ensure(cfg: dict) -> pathlib.Path:
    """注册（首次）并把端点钉到可用值。"""
    d = BASE / cfg["dir"]
    d.mkdir(parents=True, exist_ok=True)
    u = d / "usque"
    if not u.exists():
        u.symlink_to("../usque/usque")
    cp = d / "config.json"
    if not cp.exists():
        print(f"[{cfg['name']}] 注册新设备…", flush=True)
        r = subprocess.run(["./usque", "register", "-a", "-n", cfg["name"]],
                           cwd=d, capture_output=True, text=True)
        if not cp.exists():
            raise SystemExit(f"注册失败: {r.stdout[-300:]} {r.stderr[-300:]}")
    c = json.loads(cp.read_text(encoding="utf-8"))
    if c.get(cfg["key"]) != cfg["endpoint"]:
        c[cfg["key"]] = cfg["endpoint"]
        cp.write_text(json.dumps(c, indent=2) + "\n", encoding="utf-8")
    return d


def start(cfg: dict, d: pathlib.Path):
    args = ["./usque", "-c", "config.json", "socks", "--http2"]
    if cfg["family"] == "v6":
        args.append("-6")
    args += ["-s", "", "-P", "443", "-b", "127.0.0.1", "-p", str(cfg["port"])]
    lg = open(f"/tmp/cap_{cfg['family']}.log", "w")
    pr = subprocess.Popen(args + [], cwd=d, stdout=lg, stderr=subprocess.STDOUT)
    return pr


def main() -> int:
    mode = sys.argv[1]
    maxtotal = int(sys.argv[2]) if len(sys.argv) > 2 else 700
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    cfg = PLAN[mode]
    d = ensure(cfg)
    pr = start(cfg, d)
    try:
        time.sleep(8)
        ip0 = P.egress_ip(cfg["port"])
        print(f"[{mode}] 出口 = {ip0}", flush=True)
        if not ip0:
            print(f"[{mode}] 隧道未通，见 /tmp/cap_{cfg['family']}.log", flush=True)
            return 1
        ok = 0
        trans = 0
        t0 = time.time()
        death = None
        while ok < maxtotal:
            v, sec, head, ra = P.probe(cfg["port"], timeout)
            if v == "OK":
                ok += 1
                if ok % 25 == 0:
                    print(f"[{mode}] OK={ok}  用时 {int(time.time()-t0)}s", flush=True)
                continue
            if v in ("unreachable", "noprobe"):
                trans += 1
                if trans >= 8:
                    death = {"verdict": v, "head": head}
                    break
                time.sleep(2)
                continue
            death = {"verdict": v, "head": head, "ra": ra}
            break
        res = {
            "mode": mode, "entry": cfg["family"], "ip": ip0,
            "ok": ok, "trans": trans, "death": death,
            "dur_sec": round(time.time() - t0, 1),
            "ip_end": P.egress_ip(cfg["port"]),
        }
        print(json.dumps(res, ensure_ascii=False, indent=1), flush=True)
        out = pathlib.Path(f"/home/administrator/dkk-projects/auto-switch-ip/v8_cap_{mode}.json")
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    finally:
        pr.terminate()
        try:
            pr.wait(timeout=6)
        except Exception:
            pr.kill()


if __name__ == "__main__":
    raise SystemExit(main())
