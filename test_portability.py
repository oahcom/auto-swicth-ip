#!/usr/bin/env python3
"""可移植性回归：路径与部署资产不得绑死某台机器。

运行: python3 -m pytest test_portability.py -q
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

PROJECT = Path(config.PROJECT_DIR)
RUNTIME_FILES = [
    "config.py", "daemon.py", "parse_subscription.py", "probe_warp.py",
    "self_evolve_checkpoint.py", "start-auto-switch-ip.sh", "deploy/install.sh",
    "deploy/preflight.sh", "deploy/wsl-boot.conf.example",
]


def test_project_paths_resolve_inside_repo():
    """项目内文件必须落在仓库目录下：clone 到别处就该跟着走。

    在干净解释器里取默认值：同目录其他用例会在 import 时改写 CFG
    （如 test_warp_refresh 把 log_file 指向 /dev/null），进程内读会串味。
    """
    import json
    import subprocess
    code = (
        "import json,sys; sys.path.insert(0, %r); import config as c; "
        "print(json.dumps({'project': c.PROJECT_DIR, 'binary': c.CFG['singbox']['binary'], "
        "'config_file': c.CFG['singbox']['config_file'], 'log_file': c.CFG['log_file']}))"
        % str(PROJECT)
    )
    got = json.loads(subprocess.run([sys.executable, "-I", "-c", code],
                                    capture_output=True, text=True, check=True).stdout)
    project = Path(got["project"])
    assert project == PROJECT, f"PROJECT_DIR={project} 应为 {PROJECT}"
    for key in ("binary", "config_file"):
        p = Path(got[key])
        assert p.is_absolute(), f"{key} 应为绝对路径，实际 {p}"
        assert PROJECT in p.parents, f"{key}={p} 不在 {PROJECT} 下"
    assert Path(got["log_file"]).parent == PROJECT, got["log_file"]


def test_no_hardcoded_home_in_runtime_files():
    """运行期文件里不该出现 /home/<某个用户>：换用户即失效。"""
    offenders = []
    for rel in RUNTIME_FILES:
        text = (PROJECT / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"/home/[A-Za-z0-9_.-]+", text):
            offenders.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1} {m.group(0)}")
    assert not offenders, "硬编码家目录：" + "; ".join(offenders)


def test_warp_members_are_deployable():
    """每条隧道都要能被部署脚本渲染成单元：唯一 tag/port/service。"""
    members = config.CFG["warp"]["members"]
    assert members
    for key in ("tag", "port", "service"):
        vals = [m[key] for m in members]
        assert len(vals) == len(set(vals)), f"{key} 有重复: {vals}"
    assert all(str(m["service"]).endswith(".service") for m in members)


def test_env_override_expands_home():
    """`.env` 里的 `$HOME/x` 必须在两条启动路径下同解。

    systemd 的 EnvironmentFile **不展开**（实测原样传入），start 脚本 source
    则展开。二者同读一份 .env，故展开必须发生在 config 内。
    """
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import config as c; "
            "print(c.CFG['9router']['db_path'])" % str(PROJECT))
    got = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                         text=True, check=True,
                         env={**os.environ, "NINE_ROUTER_DB_PATH": "$HOME/.9router/db/data.sqlite"}
                         ).stdout.strip()
    assert "$" not in got, f"未展开: {got}"
    assert got.startswith(os.path.expanduser("~")), got


def test_install_never_links_main_usque_to_itself():
    """符号链接源是主二进制，主二进制本身不得被链接覆盖（否则 20 条隧道 ELOOP）。"""
    text = (PROJECT / "deploy/install.sh").read_text(encoding="utf-8")
    link_lines = [l for l in text.splitlines() if "ln -s" in l and "usque" in l]
    assert link_lines, "install.sh 应负责创建 usqueN 符号链接"
    for line in link_lines:
        assert '= "usque"' in line or "[ ${d%/} = .warp/usque ]" in line, \
            f"链接命令没有排除主目录自身: {line.strip()}"


def test_install_pins_interpreter_absolutely():
    """单元内解释器写绝对路径：不能依赖调用者 PATH（conda 会拿到 miniforge）。"""
    text = (PROJECT / "deploy/install.sh").read_text(encoding="utf-8")
    assert 'PYBIN="${PYTHON:-/usr/bin/python3}"' in text
    assert "command -v" not in text.split("PYBIN=")[1].split("\n")[0]


def test_fetch_binaries_pins_version_and_sha256():
    """获取脚本必须钉死版本与官方 sha256：否则装进来的是"某次下载的东西"。"""
    text = (PROJECT / "deploy/fetch-binaries.sh").read_text(encoding="utf-8")
    shas = re.findall(r"\|([0-9a-f]{64})\"", text)
    assert len(shas) >= 4, f"应覆盖 amd64/arm64 × 两个二进制，实际 {len(shas)} 条"
    assert len(set(shas)) == len(shas), "sha256 有重复，说明复制粘贴串行了"
    for want in ("sing-box-1.13.0-linux-amd64", "sing-box-1.13.0-linux-arm64",
                 "usque_4.2.1_linux_amd64", "usque_4.2.1_linux_arm64"):
        assert want in text, f"缺少 {want} 的下载地址"


def test_fetch_refuses_checksum_mismatch(tmp_path):
    """校验不符必须拒绝落盘：坏包不能变成"可执行文件已就绪"。

    本地起 http.server 供包，才能真的走到 sha256 比对那一行 —— 指向一个连不上
    的端口只会测到「下载失败」，且 curl 的重试退避要跑 87 秒。
    """
    import http.server
    import threading
    import functools

    served = tmp_path / "served"
    pkg_dir = served / "SagerNet/sing-box/releases/download/v1.13.0"
    pkg_dir.mkdir(parents=True)
    payload = pkg_dir / "sing-box-1.13.0-linux-amd64.tar.gz"
    payload.write_bytes(b"not-the-real-tarball" * 1000)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(served))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        repo = tmp_path / "repo"
        (repo / "deploy").mkdir(parents=True)
        # 全 0 期望值：真实 payload 的 sha 必然不符
        text = re.sub(r"\|[0-9a-f]{64}\"", "|" + "0" * 64 + '"',
                      (PROJECT / "deploy/fetch-binaries.sh").read_text(encoding="utf-8"))
        (repo / "deploy/fetch-binaries.sh").write_text(text)
        out = subprocess.run(["bash", str(repo / "deploy/fetch-binaries.sh")],
                             capture_output=True, text=True, timeout=60,
                             env={**os.environ, "BIN_MIRROR": f"http://127.0.0.1:{port}"})
    finally:
        srv.shutdown()

    assert out.returncode != 0, "校验不符时必须非零退出"
    assert "sha256" in out.stderr, f"应报校验失败，实际：{out.stderr[:200]}"
    assert not (repo / "sing-box").exists(), "坏包不得落盘"


def test_install_refuses_to_retarget_live_units(tmp_path):
    """从副本运行且未设 SYSTEMD_DIR 时，不得改写指向别处的现网单元。

    真实事故：`--units-only` 在 /tmp 副本里跑过一次，23 个单元全被改指 /tmp，
    而副本随时会被清理 —— 服务下次重启即崩。
    """
    fake_home = tmp_path / "home"
    unit_dir = fake_home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    (other_repo / "daemon.py").write_text("# 生产副本\n")
    (unit_dir / "auto-switch-ip.service").write_text(
        f"[Service]\nWorkingDirectory={other_repo}\n")

    # 用真实脚本配一个假 SHELL 环境：脚本按自身位置推出 DIR，所以整份复制到
    # 新的 clone 目录，才是在"另一处 clone"里运行。
    here = tmp_path / "this-clone"
    shutil.copytree(PROJECT / "deploy", here / "deploy")
    for f in ("config.py", "requirements.txt"):
        shutil.copy2(PROJECT / f, here / f)
    shutil.copy2(PROJECT / "sing-box", here / "sing-box")

    out = subprocess.run(["bash", str(here / "deploy/install.sh"), "--units-only"],
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "HOME": str(fake_home)})
    assert "拒绝覆盖生产单元" in out.stderr, \
        f"未触发防护（rc={out.returncode}）：{out.stderr[:300]}"
    # 关键：现网单元内容原封不动
    assert str(other_repo) in (unit_dir / "auto-switch-ip.service").read_text()


def test_install_detects_arch_mismatch():
    """install.sh 必须核对 ELF 架构：arm64 机器上 amd64 二进制 `-x` 也通过。"""
    text = (PROJECT / "deploy/install.sh").read_text(encoding="utf-8")
    assert "e_machine" in text or "od -An -tx1 -j18" in text, "缺少 ELF 架构探测"
    assert "fetch-binaries.sh" in text, "缺架构/缺文件时应调用获取脚本"


def test_launch_falls_back_to_v4_without_ipv6_route(tmp_path):
    """标了 v6 但本机没有 v6 路由时必须改走 v4，否则该线永远连不通还刷满日志。"""
    import subprocess
    script = tmp_path / "launch.sh"
    script.write_text((PROJECT / "deploy/usque-launch.sh").read_text(encoding="utf-8"))
    script.chmod(0o755)
    # 假 usque 只回显参数；假 ip 的退出码代表"路由查得到吗"。
    (tmp_path / "usque").write_text('#!/bin/sh\necho "$@"\n')
    (tmp_path / "usque").chmod(0o755)

    def run(route_out: str) -> str:
        # 假 ip 把路由查询结果原样打出来；退出码与真实命令一致。
        (tmp_path / "ip").write_text(
            "#!/bin/sh\n[ -n \"$ROUTE\" ] || exit 2\nprintf '%s\\n' \"$ROUTE\"\n")
        (tmp_path / "ip").chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "ROUTE": route_out}
        return subprocess.run([str(script), "1083", "-6"], cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=True).stdout

    assert "-6" in run("2606:4700:103::2 from :: via fe80::1 dev eth1 src 2001:db8::5"), \
        "有路由且有真实源地址时应走 -6"
    assert "-6" not in run(""), "无 v6 路由时必须降级走 v4"
    # 残留默认路由但源地址退化成 ::（地址已失效）也不能走 v6。
    assert "-6" not in run("2606:4700:103::2 from :: via lo src ::"), \
        "路由存在但源地址是 :: 时必须降级走 v4"


def test_deploy_renders_every_member(tmp_path):
    """部署脚本的成员渲染器必须覆盖 config 里的全部隧道，含地址族开关。"""
    import subprocess
    out = subprocess.run([sys.executable, str(PROJECT / "deploy/members.py")],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    rows = [r.split() for r in out if r.strip()]
    members = config.CFG["warp"]["members"]
    assert len(rows) == len(members)
    for row, m in zip(rows, members):
        assert row[0] == m["tag"] and int(row[1]) == m["port"] and row[2] == m["service"]
        assert row[4] == ("-6" if m.get("ipv6") else "-")


def test_start_script_pins_interpreter():
    """手动启动也要钉死解释器：conda shell 的 python3 与 systemd 单元可能不同。"""
    text = (PROJECT / "start-auto-switch-ip.sh").read_text(encoding="utf-8")
    assert 'PYBIN="${PYTHON:-/usr/bin/python3}"' in text
    assert 'nohup "$PYBIN" "$DAEMON"' in text
    assert "nohup python3" not in text


def test_preflight_covers_new_host_checks():
    """新主机预检必须覆盖 systemd/linger/Python/Bun/订阅/9router，缺一不可。"""
    text = (PROJECT / "deploy/preflight.sh").read_text(encoding="utf-8")
    for token in ("systemctl --user", "loginctl", "python", "bun", "_SUB_URL",
                  "9router", "mirrored", "fetch-binaries"):
        assert token in text, f"preflight 缺少检查项: {token}"
    # install.sh 必须调用它，且允许沙箱跳过
    install = (PROJECT / "deploy/install.sh").read_text(encoding="utf-8")
    assert "deploy/preflight.sh" in install
    assert "SKIP_PREFLIGHT" in install


def test_wsl_template_has_no_machine_paths():
    """WSL 引导模板不能带真实用户/项目绝对路径：换机即失效。"""
    text = (PROJECT / "deploy/wsl-boot.conf.example").read_text(encoding="utf-8")
    assert "systemd=true" in text
    assert "/home/" not in text and "/mnt/c/Users/" not in text


def test_readme_has_no_hardcoded_home():
    """移植文档不能把某台机器的家目录写成步骤。"""
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    assert "/home/administrator" not in text
    assert "preflight.sh" in text


def test_bun_search_includes_local_bin():
    """systemd PATH 没有 ~/.local/bin；bun 兜底搜索必须覆盖它。"""
    text = (PROJECT / "probe_warp.py").read_text(encoding="utf-8")
    assert ".local/bin/bun" in text


def test_install_renders_units_on_fresh_host(tmp_path):
    """全新主机上 UNIT_DIR 为空时不得因 pipefail 中止（sed 读不存在的单元返回 2）。

    真实回归：`SYSTEMD_DIR=/tmp/x install.sh --units-only` 首次运行时，
    `sed ... | head -1` 在 set -euo pipefail 下直接退出 2，一个单元都没生成。
    """
    here = tmp_path / "clone"
    shutil.copytree(PROJECT / "deploy", here / "deploy")
    for f in ("config.py", "requirements.txt", "sing-box"):
        shutil.copy2(PROJECT / f, here / f)
    unit_dir = tmp_path / "units"
    env = {**os.environ, "SYSTEMD_DIR": str(unit_dir),
           "HOME": str(tmp_path / "home"), "PYTHON": sys.executable,
           "SKIP_PREFLIGHT": "1"}
    out = subprocess.run(["bash", str(here / "deploy/install.sh"), "--units-only"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, f"rc={out.returncode}: {out.stderr[-400:]}"
    assert (unit_dir / "auto-switch-ip.service").exists()
    assert len(list(unit_dir.iterdir())) == len(config.CFG["warp"]["members"]) + 3
