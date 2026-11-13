#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 req= 重建一次请求的完整出口序列。

WHY 需要这个工具：

    排查「这条 429 到底是谁的」时，最大的障碍不是数据不够，而是**一次用户请求在
    日志里是散的**。一次请求可能被切 3 次出口，产生 3 行 [ASIP] rotate、若干行
    429 正文、一行 📊 DONE，时间戳相差几十秒，中间还夹着别的请求的行。靠时间戳
    对齐必然对错（2026-11-13 就因为对错端口，一度算出「单 IP 承载 10217 发」）。

    之前唯一的粘合剂是 warn 行的 `(req=xxx)` 后缀 —— 但 📊 DONE 不带 req=
    （2026-11-13 03:34 已补），于是「这个请求最后落在哪个出口、成功还是失败」
    这一头是断的，只剩中间过程。这个工具就是把断掉的两头接起来。

字段可靠性（读代码得出，别凭字段名猜）：

    to_ip        ✅ 可靠。daemon 主动下发的跳转目标（switch_to_egress），
                    就是这条请求**接下来要走的出口**。
    ip           ⚠️ 不可靠。来自 res.offending_egress || res.active_egress。
                    asipRotate.js:334 在 daemon 判定「这个地址已被换走」
                    （already_retired）时退回 active_egress —— 那读的是**换完之后**
                    的新地址，不是失败那一发打中的地址。只能当「daemon 认为是它」，
                    不能当「失败发生在它身上」。
    verdict      ⚠️ 同上，跟 ip 绑定的结论，仅供参考。
    to_line      ✅ 可靠，随 to_ip 一起下发。

    → 重建「A→B→C」时用 [起点] + to_ip 链，不要用 ip= 拼。

数据源：

    journalctl -u 9router（默认，近 N 小时），或 --file 指定导出的日志文件。
    兼容两种行首：`Nov 13 03:36:20 host npm[pid]: ` 与裸 `[03:36:20] `。

用法：

    python3 tools/trace_req.py --req wdy7wjt0g          # 单请求时间线
    python3 tools/trace_req.py --top 10 --since 3h      # 换线最多的 10 个请求
    python3 tools/trace_req.py --stats --since 6h       # 全局分布
    python3 tools/trace_req.py --file /tmp/n9.log --top 5
"""

import argparse
import re
import subprocess
import sys
from collections import Counter, defaultdict

# journald 前缀：Nov 13 03:36:20 HOST npm[216852]: [03:36:20] ...
JD_PREFIX = re.compile(r"^[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\S+?:\s*")
TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")
KV = re.compile(r"(\w+)=(\S+)")
# req id 单独用紧凑字符集提，不能用 KV 的 \S+：
#   [EGRESS] retry ... (req=wdyejb01m)   ← 值后面紧跟右括号，\S+ 会把 ')' 吃进去，
#   于是同一个请求被拆成 "wdyejb01m" 和 "wdyejb01m)" 两个 trace，事件各丢一半。
REQ_ID = re.compile(r"req=([A-Za-z0-9_-]+)")

REQ_POST = re.compile(r"▶\s*POST\s+(.+?)\s*·")
REQ_DONE = re.compile(r"📊\s*DONE\s+(.*)$")
REQ_ROTATE = re.compile(r"\[ASIP\]\s*rotate\s")
REQ_FAIL = re.compile(r"✗")
# [EGRESS] retry 1/2 429 -> accounted on http://127.0.0.1:7897 (ok) (req=x)
#   → 这条 429 记在**哪个端口**头上。它和 [ASIP] rotate 的 to_url 是两回事：
#     rotate 说"接下来改走谁"，retry 说"这次失败记给了谁"。排查归因只看这条。
REQ_RETRY = re.compile(
    r"\[EGRESS\]\s*retry\s+(\d+)/(\d+)\s+(\d+)\s*->\s*accounted on\s+(\S+)\s*\((\w+)\)")
# [EGRESS] asip rotated egress (ok) -> retry 1/2 on fresh connection (req=x)
REQ_ROTATED = re.compile(
    r"\[EGRESS\]\s*asip rotated egress\s*\((\w+)\)\s*->\s*retry\s+(\d+)/(\d+)")
# [EGRESS] 429 re-session replay -> 200 (false positive); skip accounting (req=x)
# P0-3 原目标：同出口换 session 复测的结论。只进事件序列，不参与 hop/归因链 ——
# 它既不是一次换线，也不是一次报障记账。
REQ_RESESSION = re.compile(r"\[EGRESS\]\s*429 re-session replay(.*)$")


def read_lines(args):
    """返回日志行迭代器（已去掉 journald 前缀）。"""
    if args.file:
        with open(args.file, "rb") as f:
            for raw in f:
                yield raw.decode("utf-8", "replace").rstrip("\n")
        return
    cmd = ["journalctl", "-u", "9router", "--no-pager", "-o", "cat"]
    if args.since:
        cmd += ["--since", "-" + args.since]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for raw in p.stdout.split(b"\n"):
        yield raw.decode("utf-8", "replace")


def strip_prefix(line):
    t = JD_PREFIX.sub("", line)
    return t.lstrip()


def seg64(ip):
    if not ip or ":" not in ip:
        return ip
    return ":".join(ip.split(":")[:4])


def ok(v):
    """日志里缺失字段写成 '-'，别让它参与判断。"""
    return v if v and v != "-" else None


def is_ip6(x):
    """纯 IPv6 字面量。必须用字符集判，不能用「含冒号」——
    代理池节点长这样：ttll20-ooi.dankenet.help:64657-any，也含冒号。"""
    return bool(x) and re.fullmatch(r"[0-9a-fA-F:]{4,}", x) is not None


def egress_id(h):
    """这条 rotate 把请求送到哪个出口。

    两种换线共用 /rotate 端点，出口标识放在不同字段：
      WARP 槽位轮换  → to_ip   有值（真实地址）
      代理池节点轮换 → to_ip=- ，标识在 to_node / to_line（主机名）
    所以按 to_ip → to_node → to_line 的顺序取第一个有值的。
    """
    return ok(h.get("to_ip")) or ok(h.get("to_node")) or ok(h.get("to_line"))


def short_port(p):
    if not p:
        return "-"
    return p.rstrip("/").rsplit(":", 1)[-1] if ":" in p else p


class ReqTrace:
    __slots__ = ("req", "start_ts", "post", "hops", "events", "done", "fails", "last_ts")

    def __init__(self, req):
        self.req = req
        self.start_ts = None
        self.last_ts = None
        self.post = None
        self.hops = []      # 只看 [ASIP] rotate
        self.events = []    # 全事件，按日志顺序（rotate / rotated / retry）
        self.done = None    # DONE 行正文
        self.fails = 0

    def chain(self):
        """出口链：每次 rotate 的下发目标。

        起点（POST 时的出口）日志里没有，所以这条链从「第一次失败后去了哪」开始，
        长度 = 换线次数，不含起点。
        """
        out = []
        for h in self.hops:
            eid = egress_id(h)
            if eid and (not out or out[-1] != eid):
                out.append(eid)
        return out

    def is_success(self):
        return self.done is not None

    def render(self, show_events=False):
        ts_span = f"{self.start_ts or '?'} → {self.last_ts or '?'}"
        status = "成功" if self.is_success() else ("失败" if self.fails else "未完成/仍在途")
        head = f"req={self.req}   [{ts_span}]  {status}   换线 {len(self.hops)} 次"
        lines = [head]
        if self.post:
            lines.append("  " + self.post)
        for i, h in enumerate(self.hops, 1):
            borrowed = " [借道]" if h.get("borrowed") else ""
            lines.append(
                "  hop%d  %s  :%s  reason=%s%s"
                % (i, h.get("_ts", "?"), short_port(h.get("port")),
                   h.get("reason", "?"), borrowed)
            )
            who_line = ok(h.get("line"))
            who_ip = ok(h.get("ip"))
            if who_line or who_ip:
                lines.append(
                    "        判定的问题线: %s (%s)  verdict=%s"
                    % (who_line or "-", who_ip or "-", h.get("verdict", "n/a"))
                )
            if h.get("acct_port"):
                lines.append(
                    "        这次 %s 记在 :%s (%s, retry %s)"
                    % (h.get("acct_code", "?"), short_port(h["acct_port"]),
                       h.get("acct_status", "?"), h.get("acct_try", "?"))
                )
            nxt = egress_id(h)
            if nxt:
                lines.append("        → 下一跳: %s" % nxt)
        if self.done:
            lines.append("  DONE  %s" % self.done)
        if show_events and self.events:
            lines.append("  ── 事件序列（日志原序）──")
            for ev in self.events:
                k = ev.get("_kind")
                ts = ev.get("_ts", "?")
                if k == "rotate":
                    lines.append("  %s  rotate    :%s → %s"
                                 % (ts, short_port(ev.get("port")), egress_id(ev) or "-"))
                elif k == "rotated":
                    lines.append("  %s  rotated   (%s)" % (ts, ev.get("status")))
                elif k == "resession":
                    lines.append("  %s  resession %s" % (ts, ev.get("detail", "")))
                elif k == "retry":
                    lines.append("  %s  retry %s/%s %s  记在 :%s (%s)"
                                 % (ts, ev.get("n"), ev.get("m"), ev.get("code"),
                                    short_port(ev.get("port")), ev.get("status")))
        chain = self.chain()
        if chain:
            if all(is_ip6(x) for x in chain):
                lines.append("  ── 下发链路 %d 个出口 / %d 个 /64"
                             % (len(chain), len({seg64(x) for x in chain})))
            else:
                lines.append("  ── 下发链路 %d 个出口（代理池节点轮换）" % len(chain))

        # ip= 是 res.offending_egress || res.active_egress。daemon 判 already_retired
        # 时会退回 active_egress —— 读的是「换完之后」的活动线，不是失败那一发打中的
        # 地址。表现就是：一个请求 3 次换线，3 个 hop 的 ip 全一样。看到就提醒一句，
        # 免得又拿它去数「哪个 IP 承载了多少发」。
        ipset = {h.get("ip") for h in self.hops if ok(h.get("ip"))}
        if len(self.hops) >= 2 and len(ipset) == 1:
            lines.append("  ⚠️ %d 个 hop 的 ip= 完全相同（%s）：疑似 active_egress 兜底，"
                         "不可当「失败那一发打中的地址」"
                         % (len(self.hops), ipset.pop()))
        return "\n".join(lines) + "\n"


def build(traces, line, state):
    t = strip_prefix(line)
    if "req=" not in t:
        return
    kv = dict(KV.findall(t))
    mreq = REQ_ID.search(t)
    req = mreq.group(1) if mreq else kv.get("req")
    if not req:
        return

    # [ASIP] rotate 是 console.warn 直出，行首没有 [HH:MM:SS]。日志是顺序的，
    # 继承上一行的时间戳即可（误差 = 同秒内的行数，换线场景只关心秒级）。
    mts = TS.match(t)
    if mts:
        ts = mts.group(1)
        state["ts"] = ts
    else:
        ts = state.get("ts")

    tr = traces.get(req)
    if tr is None:
        tr = traces[req] = ReqTrace(req)
    if tr.start_ts is None and ts:
        tr.start_ts = ts
    if ts:
        tr.last_ts = ts

    if REQ_ROTATE.search(t):
        h = dict(kv)
        h["_ts"] = ts
        h["_kind"] = "rotate"
        tr.hops.append(h)
        tr.events.append(h)
        return
    mres = REQ_RESESSION.search(t)
    if mres:
        # 行尾的 (req=xxx) 只是关联键，别混进结论文本。
        detail = re.sub(r"\s*\(req=[A-Za-z0-9_-]+\)\s*$", "",
                        mres.group(1)).strip()
        tr.events.append({"_kind": "resession", "_ts": ts, "detail": detail})
        return
    mretry = REQ_RETRY.search(t)
    if mretry:
        n, m, code, port, status = mretry.groups()
        ev = {"_kind": "retry", "_ts": ts, "n": n, "m": m, "code": code,
              "port": port, "status": status}
        tr.events.append(ev)
        # 挂到最近一个还没记账的 hop 上：日志顺序是 rotate → rotated → retry。
        for h in reversed(tr.hops):
            if "acct_port" not in h:
                h["acct_port"] = port
                h["acct_status"] = status
                h["acct_code"] = code
                h["acct_try"] = "%s/%s" % (n, m)
                break
        return
    mrot = REQ_ROTATED.search(t)
    if mrot:
        tr.events.append({"_kind": "rotated", "_ts": ts, "status": mrot.group(1)})
        return
    mpost = REQ_POST.search(t)
    if mpost:
        tr.post = mpost.group(1)
        return
    mdone = REQ_DONE.search(t)
    if mdone:
        tr.done = mdone.group(1).strip()
        return
    if REQ_FAIL.search(t):
        tr.fails += 1


def print_stats(traces, top_n):
    total = len(traces)
    if not total:
        print("窗口内没有可识别的 req（📊 DONE 补 req= 于 2026-11-13 03:34 上线，"
              "更早的窗口只有 rotate 行带 req=）。")
        return

    dist = Counter(len(t.hops) for t in traces.values())
    succ = [t for t in traces.values() if t.is_success()]
    fail = [t for t in traces.values() if not t.is_success() and t.fails]
    inport = [t for t in traces.values() if t.is_success() and not t.hops]
    print("窗口内请求数：%d（成功 %d / 失败 %d / 未判 %d）"
          % (total, len(succ), len(fail), total - len(succ) - len(fail)))
    print("\n换线次数分布：")
    for k in sorted(dist):
        print("  %2d 次换线 : %5d" % (k, dist[k]))
    if succ:
        one = sum(1 for t in succ if not t.hops)
        print("\n成功请求中：一次就中 %d（%.1f%%），换过线才成功 %d（%.1f%%）"
              % (one, 100.0 * one / len(succ), len(succ) - one,
                 100.0 * (len(succ) - one) / len(succ)))
    borrowed = sum(1 for t in traces.values() for h in t.hops if h.get("borrowed"))
    allhops = sum(len(t.hops) for t in traces.values())
    if allhops:
        print("换线总次数 %d，其中借道 %d（%.1f%%）" % (allhops, borrowed, 100.0 * borrowed / allhops))

    chains = [t for t in traces.values() if t.chain()]
    if chains:
        segs = [len({seg64(x) for x in t.chain()}) for t in chains]
        print("有下发链路的请求 %d，平均跨 %.2f 个 /64" % (len(chains), sum(segs) / len(segs)))

    print("\n换线最多的 %d 个请求：" % top_n)
    for t in sorted(traces.values(), key=lambda x: -len(x.hops))[:top_n]:
        print("  %-12s 换线 %d 次  成功=%s  %s"
              % (t.req, len(t.hops), t.is_success(), t.post or ""))


def main():
    ap = argparse.ArgumentParser(description="按 req= 重建请求出口序列")
    ap.add_argument("--req", help="只看这个 req id")
    ap.add_argument("--top", type=int, default=10, help="列出换线最多的 N 个请求")
    ap.add_argument("--since", default="6h", help="journalctl 时间窗，如 3h / 2026-11-13")
    ap.add_argument("--file", help="从日志文件读，而不是 journalctl")
    ap.add_argument("--stats", action="store_true", help="输出全局分布而不是单条时间线")
    ap.add_argument("--verbose", action="store_true", help="单请求模式下同时打印未归类行")
    ap.add_argument("--events", action="store_true",
                    help="额外打印原始事件序列（rotate / rotated / retry 按日志顺序）")
    args = ap.parse_args()

    traces = {}
    state = {"ts": None}
    for line in read_lines(args):
        if "req=" in line:
            build(traces, line, state)

    if args.stats or not args.req:
        print_stats(traces, args.top)
        return 0

    tr = traces.get(args.req)
    if not tr:
        print("窗口内没找到 req=%s（日志保留范围可能不够；用 --since 放宽）" % args.req)
        return 1
    sys.stdout.write(tr.render(show_events=args.events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
