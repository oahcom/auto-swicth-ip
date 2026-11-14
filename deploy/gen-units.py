#!/usr/bin/env python3
"""systemd 单元生成器 —— install.sh 单元模板的单一真相源。

WHY 本文件存在（2026-11-14 版本化基线，T5/W1）：
历史上单元内容以 heredoc 写死在 deploy/install.sh 里，而线上单元被手改后与
模板各自演化（手改 --interval、SINGBOX_SECRET/NINE_ROUTER_PASSWORD 直写单元、
KillMode=process 与 TZ 缺失靠 drop-in 补、10-precheck 落后于仓库版）——
「配置双主人」在 unit 层重演，事后没人说得清哪个是权威。本生成器是唯一
渲染来源，两种用法覆盖写入与对照：
  install.sh --units-only  →  gen-units.py --write   （写入单元目录）
  日常巡检 / 差异对照报告  →  gen-units.py --check   （只读对照，不改线上）

原作用边界（只许增强不许削弱，install.sh 的护栏不归本文件管）：
本文件**只负责渲染单元文本**。防误覆盖闸（/tmp 副本拒覆盖现网单元）、
precheck drop-in 下发、sudoers/特权包装、enable/restart、注册失败拒成功
等护栏全部留在 install.sh，语义一字不动。

只渲染 install.sh 负责的单元族（auto-switch-ip.service / asi-selfcheck.* /
usque-warp*.service）。warp-wg-*（deploy/wg-lines-install.sh）与 usque-h2-*
（deploy/render_h2_lines.py → h2-install.sh）各有自己的生成器，是已知的
另外两个 unit 来源——收编进本生成器属 W4 治理项，本波不动（见
docs/unit-drift-report-20261114.md 多源说明）。

用法：
  python3 deploy/gen-units.py --write --unit-dir DIR [--project-dir DIR]
      [--run-user U] [--pybin P] [--members-stdin]
  python3 deploy/gen-units.py --check --unit-dir DIR [...]
  python3 deploy/gen-units.py --list [--members-stdin]

成员行输入（tag port service dir flag，5 字段）：--members-stdin 时从 stdin
读；否则自己调 deploy/members.py 并按 install.sh 同口径过滤（NF==5，把
config import 期告警行挡在外面）。install.sh 传 stdin 保证「同一份成员表
既用于注册又用于渲染」，不在渲染期二次采样。

退出码：成功 0；--check 发现漂移 1；输入/参数错误 2。
"""
import argparse
import difflib
import os
import subprocess
import sys
import tempfile

OWNED_FILES = ("auto-switch-ip.service", "asi-selfcheck.service", "asi-selfcheck.timer")
OWNED_PREFIXES = ("auto-switch-ip", "asi-selfcheck", "usque-warp")


def render_main_service(project_dir, run_user, pybin):
    return "auto-switch-ip.service", (
        "[Unit]\n"
        "Description=auto-switch-ip daemon — monitor 9router errors + rotate sing-box proxy nodes\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "# 以 administrator 而非 root 运行：config.py 用 pwd.getpwuid(os.getuid()).pw_dir\n"
        "# 取家目录，root 会得到 /root，9router 的库、.env、.warp 全部找不到。\n"
        "# 代价是重启系统级单元要提权，走 /usr/local/bin/asip-unit（sudoers 白名单）。\n"
        f"User={run_user}\n"
        f"Group={run_user}\n"
        f"ExecStart={pybin} {project_dir}/daemon.py\n"
        f"WorkingDirectory={project_dir}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "# sing-box 由 daemon 用 Popen(start_new_session=True) 拉起，仍留在本 unit 的 cgroup 里。\n"
        "# 默认 KillMode=control-group 会让每次 restart 连带杀掉 sing-box → daemon 置 bypass 直连\n"
        "# → opencode 看到本地 IP 直接 403。实测三次重启各触发 ~50-100 条 403。\n"
        "KillMode=process\n"
        "# 夜间刷新锁按**本地小时**判窗口（daemon._night_active 用 localtime）。\n"
        "# 主机时区若被改成 UTC（WSL 重建/容器常见），20:00→08:00 会整体偏移 8h，\n"
        "# 变成白天锁、夜里不锁 —— 那正是这套锁要修的事故形态，且只在特定时段可见。\n"
        "# 钉死在 Asia/Shanghai 与上游免费层 08:00 配额重置同源。\n"
        "Environment=TZ=Asia/Shanghai\n"
        f"EnvironmentFile=-{project_dir}/.env\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_selfcheck_service(project_dir, run_user, pybin):
    return "asi-selfcheck.service", (
        "[Unit]\n"
        "Description=auto-switch-ip 自检引擎 — 巡检并写入 .monitor_state.json\n"
        "After=multi-user.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"User={run_user}\n"
        f"Group={run_user}\n"
        f"ExecStart={pybin} {project_dir}/self_evolve_checkpoint.py\n"
        f"WorkingDirectory={project_dir}\n"
        f"EnvironmentFile=-{project_dir}/.env\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_selfcheck_timer():
    # install.sh 里这段 heredoc 是引号版（<<'EOF'），内容字面量、不做变量展开。
    return "asi-selfcheck.timer", (
        "[Unit]\n"
        "Description=auto-switch-ip 自检定时器 — 每 10 分钟触发 asi-selfcheck.service\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*:0/10\n"
        "Persistent=true\n"
        "RandomizedDelaySec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_member_unit(tag, port, svc, line_dir, fam, project_dir, run_user):
    return svc, (
        "[Unit]\n"
        f"Description=Cloudflare WARP (MASQUE) SOCKS5 proxy {tag} for auto-switch-ip\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={run_user}\n"
        f"Group={run_user}\n"
        f"WorkingDirectory={project_dir}/.warp/{line_dir}\n"
        f"ExecStart={project_dir}/deploy/usque-launch.sh {port} {fam}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def load_members(args):
    """返回 [(tag, port, svc, dir, fam), ...]，口径与 install.sh 一致：NF==5。"""
    if args.members_stdin:
        raw = sys.stdin.read()
    else:
        members_py = os.path.join(args.project_dir, "deploy", "members.py")
        proc = subprocess.run([args.pybin, members_py], capture_output=True, text=True)
        raw = proc.stdout
    rows = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 5:
            rows.append(tuple(parts))
    return rows


def render_all(args, members):
    units = [render_main_service(args.project_dir, args.run_user, args.pybin),
             render_selfcheck_service(args.project_dir, args.run_user, args.pybin),
             render_selfcheck_timer()]
    for tag, port, svc, line_dir, fam in members:
        units.append(render_member_unit(tag, port, svc, line_dir, fam,
                                        args.project_dir, args.run_user))
    return units


# ---- --check 的结构化对照：行级差异 + 键级差异（键级供差异报告直接引用） ----

def parse_unit(text):
    """把单元文本解析为 {(section, key): value}；注释与空行不入表。"""
    parsed = {}
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            parsed[(section, key.strip())] = val.strip()
    return parsed


def key_diff(expected, actual):
    """返回 (only_expected, only_actual, changed)；changed 为 (键, 期望值, 实际值)。"""
    ek, ak = parse_unit(expected), parse_unit(actual)
    only_e = {k: ek[k] for k in ek.keys() - ak.keys()}
    only_a = {k: ak[k] for k in ak.keys() - ek.keys()}
    changed = {k: (ek[k], ak[k]) for k in ek.keys() & ak.keys() if ek[k] != ak[k]}
    return only_e, only_a, changed


def check_one(name, expected, unit_dir):
    """对照单个单元；返回漂移条目列表（空=一致）。"""
    path = os.path.join(unit_dir, name)
    if not os.path.exists(path):
        return [f"{name}: 线上缺失（生成器会产出该单元）"]
    with open(path, encoding="utf-8") as f:
        actual = f.read()
    if actual == expected:
        return []
    entries = [f"{name}: 内容与生成器不一致"]
    only_e, only_a, changed = key_diff(expected, actual)
    for (sec, key), val in sorted(only_e.items()):
        entries.append(f"    缺键 {sec} {key}（模板值: {val}）")
    for (sec, key), val in sorted(only_a.items()):
        entries.append(f"    多键 {sec} {key} = {val}（模板无此键）")
    for (sec, key), (ev, av) in sorted(changed.items()):
        entries.append(f"    值漂移 {sec} {key}: 模板[{ev}] vs 线上[{av}]")
    if not (only_e or only_a or changed):
        entries.append("    仅注释/空白差异（语义键一致）")
    return entries


def cmd_check(args, members):
    units = render_all(args, members)
    drift = []
    for name, content in units:
        drift.extend(check_one(name, content, args.unit_dir))
    # 生成器拥有前缀下、但不在本次生成集里的现存单元（成员表变了没清理 / 手工产物）
    rendered = {name for name, _ in units}
    if os.path.isdir(args.unit_dir):
        for fname in sorted(os.listdir(args.unit_dir)):
            if not fname.endswith((".service", ".timer")):
                continue
            if not fname.startswith(OWNED_PREFIXES):
                continue
            if fname in rendered:
                continue
            drift.append(f"{fname}: 前缀归生成器但不在当前成员表（陈旧成员残留或手工产物）")
    if drift:
        print(f"✗ 单元差异 {len(drift)} 条（--unit-dir {args.unit_dir}）")
        for line in drift:
            print(line)
        return 1
    print(f"✓ 生成器单元集与 {args.unit_dir} 完全一致（{len(units)} 个单元）")
    return 0


def cmd_write(args, members):
    units = render_all(args, members)
    os.makedirs(args.unit_dir, exist_ok=True)
    for name, content in units:
        path = os.path.join(args.unit_dir, name)
        # 原子替换：先写同目录临时文件再 os.replace，断电/中断不会留下半个单元。
        fd, tmp = tempfile.mkstemp(dir=args.unit_dir, prefix="." + name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    print(f"已生成 {len(units)} 个单元 → {args.unit_dir}")
    return 0


def cmd_list(args, members):
    for name, _ in render_all(args, members):
        print(name)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="渲染并写入 --unit-dir")
    mode.add_argument("--check", action="store_true", help="只读对照 --unit-dir，输出漂移")
    mode.add_argument("--list", action="store_true", help="只打印将生成的单元名")
    p.add_argument("--unit-dir", help="单元目录（SYSTEMD_DIR 同义）")
    p.add_argument("--project-dir", default=None, help="项目根（默认取本文件上两级）")
    p.add_argument("--run-user", default=None, help="单元 User/Group（默认当前用户）")
    p.add_argument("--pybin", default=os.environ.get("PYTHON", "/usr/bin/python3"))
    p.add_argument("--members-stdin", action="store_true",
                   help="成员行从 stdin 读（install.sh 传入，保证与注册同一份表）")
    args = p.parse_args()
    if args.project_dir is None:
        args.project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.run_user is None:
        args.run_user = os.environ.get("ASIP_RUN_USER") or os.environ.get("USER") or ""
        if not args.run_user:
            import pwd
            args.run_user = pwd.getpwuid(os.getuid()).pw_name
    if (args.write or args.check) and not args.unit_dir:
        p.error("--write/--check 需要 --unit-dir")
    members = load_members(args)
    if args.write:
        return cmd_write(args, members)
    if args.check:
        return cmd_check(args, members)
    return cmd_list(args, members)


if __name__ == "__main__":
    sys.exit(main())
