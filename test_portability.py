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
    "deploy/asip-cli.sh", "deploy/asip-cli-conf.sh",
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
    shutil.copytree(PROJECT / "asip", here / "asip")
    for f in ("config.py", "requirements.txt"):
        shutil.copy2(PROJECT / f, here / f)
    shutil.copy2(PROJECT / "sing-box", here / "sing-box")

    out = subprocess.run(["bash", str(here / "deploy/install.sh"), "--units-only"],
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "HOME": str(fake_home),
                              "ASIP_HOME_DIR": str(fake_home)})
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


def test_asip_cli_follows_actual_unit_scope(tmp_path):
    """CLI 的 systemctl 作用域必须跟着单元的落点，不能恒用 --user。

    用户级单元迁到系统级后，`systemctl --user stop <unit>` 会返回 0 而什么也没做 ——
    `asip warp stop` 报成功、隧道照跑，比报错更难查。
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    conf = tmp_path / "cli.env"
    env = {**os.environ, "ASIP_CLI_ENV": str(conf), "ASIP_HOME_DIR": str(fake_home),
           "PYTHON": sys.executable, "ASIP_SYSTEM_UNIT_DIR": str(tmp_path / "nope")}
    subprocess.run(["bash", str(PROJECT / "deploy/asip-cli-conf.sh")],
                   check=True, capture_output=True, timeout=60, env=env)
    text = conf.read_text(encoding="utf-8")
    assert "ASIP_SYSTEMD_SCOPE=user" in text, "单元不在 /etc 时应退回用户级作用域"

    (tmp_path / "nope").mkdir()
    (tmp_path / "nope/auto-switch-ip.service").write_text("[Service]\n")
    subprocess.run(["bash", str(PROJECT / "deploy/asip-cli-conf.sh")],
                   check=True, capture_output=True, timeout=60, env=env)
    text = conf.read_text(encoding="utf-8")
    assert "ASIP_SYSTEMD_SCOPE=system" in text, "系统级单元存在时必须用 --system"


def test_install_renders_units_on_fresh_host(tmp_path):
    """全新主机上 UNIT_DIR 为空时不得因 pipefail 中止（sed 读不存在的单元返回 2）。

    真实回归：`SYSTEMD_DIR=/tmp/x install.sh --units-only` 首次运行时，
    `sed ... | head -1` 在 set -euo pipefail 下直接退出 2，一个单元都没生成。
    """
    here = tmp_path / "clone"
    shutil.copytree(PROJECT / "deploy", here / "deploy")
    shutil.copytree(PROJECT / "asip", here / "asip")
    for f in ("config.py", "requirements.txt", "sing-box"):
        shutil.copy2(PROJECT / f, here / f)
    unit_dir = tmp_path / "units"
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    # ASIP_HOME_DIR 而非 HOME：脚本按 passwd 取家目录，只改 HOME 挡不住它把
    # 设置和 asip 入口装进真实用户目录。
    env = {**os.environ, "SYSTEMD_DIR": str(unit_dir), "ASIP_HOME_DIR": str(fake_home),
           "HOME": str(fake_home), "PYTHON": sys.executable,
           "SKIP_PREFLIGHT": "1"}
    out = subprocess.run(["bash", str(here / "deploy/install.sh"), "--units-only"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, f"rc={out.returncode}: {out.stderr[-400:]}"
    assert (unit_dir / "auto-switch-ip.service").exists()
    assert len(list(unit_dir.iterdir())) == len(config.CFG["warp"]["members"]) + 3
    # 部署必须顺带产出 asip 设置与入口：CLI 不含任何站点设置，缺了它整条运维
    # 命令链（日志/换线/隧道）都不可用，而这些正是部署后第一个要用的东西。
    assert (tmp_path / "home/.config/asip/cli.env").exists(), "未生成 asip 设置"
    assert (tmp_path / "home/.local/bin/asip").is_symlink(), "未安装 asip 入口"


def test_rehearsal_settings_point_at_the_rehearsal_units(tmp_path):
    """SYSTEMD_DIR 试跑时，生成的设置必须指向那个目录，而不是 /etc 的真实单元。

    真实事故路径：`SYSTEMD_DIR=/tmp/x ./deploy/install.sh --units-only` 把单元写进
    /tmp/x，但如果设置照旧探测 /etc（真实单元也在那），演练完随手跑一次
    `asip warp stop` 就会去屏蔽生产单元 —— 演练反而动了生产。
    """
    here = tmp_path / "clone"
    shutil.copytree(PROJECT / "deploy", here / "deploy")
    shutil.copytree(PROJECT / "asip", here / "asip")
    for f in ("config.py", "requirements.txt", "sing-box"):
        shutil.copy2(PROJECT / f, here / f)
    unit_dir = tmp_path / "rehearsal-units"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "SYSTEMD_DIR": str(unit_dir), "ASIP_HOME_DIR": str(fake_home),
           "HOME": str(fake_home), "PYTHON": sys.executable, "SKIP_PREFLIGHT": "1"}
    out = subprocess.run(["bash", str(here / "deploy/install.sh"), "--units-only"],
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, f"rc={out.returncode}: {out.stderr[-400:]}"
    text = (fake_home / ".config/asip/cli.env").read_text(encoding="utf-8")
    assert f"ASIP_UNIT_DIR={unit_dir}" in text, \
        f"设置未指向演练的单元目录，实际：{[l for l in text.splitlines() if 'UNIT_DIR' in l]}"
    assert "ASIP_SYSTEMD_SCOPE=user" in text, \
        "/tmp 下的单元不是系统级：作用域应为 user，否则会去 systemctl --system 操作真实单元"
    assert "/etc/systemd/system" not in text.split("ASIP_UNIT_DIR=")[1].split("\n")[0]


def test_asip_cli_reads_everything_from_generated_settings(tmp_path):
    """asip CLI 不得自带路径/端口/单元清单：换目录或改 members 后必须自动跟上。

    设置文件由 config.py 推导，所以断言 CLONE 目录与 CLONE 里的成员出现在产物中，
    并且源仓库路径不出现 —— 后者会被"手抄一份真路径"的写法满足。
    """
    here = tmp_path / "clone"
    shutil.copytree(PROJECT / "deploy", here / "deploy")
    shutil.copy2(PROJECT / "config.py", here / "config.py")
    out_env = tmp_path / "cli.env"
    out = subprocess.run(["bash", str(here / "deploy/asip-cli-conf.sh")], text=True,
                         capture_output=True, timeout=60,
                         env={**os.environ, "ASIP_CLI_ENV": str(out_env),
                              "PYTHON": sys.executable})
    assert out.returncode == 0, out.stderr[-400:]
    text = out_env.read_text(encoding="utf-8")
    assert str(here) in text, "设置未随 clone 目录变化"
    assert str(PROJECT) not in text, "设置里混入了源仓库路径（说明是写死的）"
    members = config.CFG["warp"]["members"]
    assert text.count("usque-warp") >= len(members), "隧道清单未写全"
    for key in ("ASIP_LOG", "ASIP_SECRET_FILE", "ASIP_CLASH_API", "ASIP_WARP_SELECTOR",
                "ASIP_UNIT_DIR", "ASIP_IP_USED_FILE", "ASIP_CYCLES_FILE", "ASIP_PY",
                "ASIP_SOCKS_HOST"):
        assert f"{key}=" in text, f"设置缺少 {key}"

    # CLI 侧：源文件里不得再有写死的绝对路径或隧道端口
    cli = (PROJECT / "deploy/asip-cli.sh").read_text(encoding="utf-8")
    assert "/home/" not in cli, "asip-cli.sh 出现写死的家目录"
    assert "usque-warp.service" not in cli, "asip-cli.sh 自带隧道清单（应从设置读取）"
    assert "127.0.0.1:" not in cli, "asip-cli.sh 写死了探测主机（应从 ASIP_SOCKS_HOST 读取）"
    assert 'ASIP_CLI_ENV' in cli and '. "$ASIP_CLI_ENV"' in cli, "CLI 未加载设置文件"


def _fake_bin(tmp_path, name, script):
    """在 tmp_path/bin 里放一个记录 argv 的桩，返回 (bin_dir, log_path)。"""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / f"{name}.argv"
    stub = bindir / name
    stub.write_text("#!/bin/bash\nprintf '%s\\n' \"$*\" >> " + str(log) + "\n" + script + "\n",
                    encoding="utf-8")
    stub.chmod(0o755)
    return bindir, log


def _cli_env(tmp_path, **overrides):
    """按真实设置生成一份隔离 cli.env；不读生产文件，避免用例受本机部署影响。"""
    conf = tmp_path / "cli.env"
    out = subprocess.run(["bash", str(PROJECT / "deploy/asip-cli-conf.sh")], text=True,
                         capture_output=True, timeout=60,
                         env={**os.environ, "ASIP_CLI_ENV": str(conf),
                              "PYTHON": sys.executable,
                              "ASIP_HOME_DIR": str(tmp_path / "home")})
    assert out.returncode == 0, out.stderr[-400:]
    text = conf.read_text(encoding="utf-8")
    for k, v in overrides.items():
        text = re.sub(rf"^{k}=.*$", f"{k}={v}", text, count=1, flags=re.M)
    conf.write_text(text, encoding="utf-8")
    return conf


def test_asip_cli_uses_configured_socks_host_and_every_member(tmp_path):
    """探测主机与成员清单必须来自设置：用桩 curl 记录 argv，断言真实用上。

    字符串断言（"源码里没有 127.0.0.1:"）挡不住 `localhost:{}`、`SOCKS_HOST=127.0.0.1`
    这类等价硬编码，也挡不住「PORTS 写死前 10 条」这个真实事故。行为断言两种都能抓。
    """
    members = config.CFG["warp"]["members"]
    ports = [str(m["port"]) for m in members]
    env = _cli_env(tmp_path, ASIP_SOCKS_HOST="10.9.8.7",
                   ASIP_CLASH_API="http://127.0.0.1:1")
    bindir, log = _fake_bin(tmp_path, "curl", "exit 0")

    subprocess.run(["bash", str(PROJECT / "deploy/asip-cli.sh"), "warp", "status"],
                   capture_output=True, text=True, timeout=120,
                   env={**os.environ, "ASIP_CLI_ENV": str(env),
                        "PATH": f"{bindir}:{os.environ['PATH']}"})
    argv = log.read_text(encoding="utf-8") if log.exists() else ""
    assert argv, "桩 curl 一次都没被调用（探测路径没跑）"
    assert "10.9.8.7" in argv, f"探测没走设置的 ASIP_SOCKS_HOST：{argv[:300]}"
    assert "127.0.0.1:1" in argv or "10.9.8.7" in argv
    probed = [p for p in ports if f"10.9.8.7:{p}" in argv]
    assert len(probed) == len(ports), \
        f"探测了 {len(probed)}/{len(ports)} 条隧道（历史 bug：写死前 10 条）：{probed}"


def test_asip_cli_systemctl_scope_follows_settings(tmp_path):
    """CLI 的 systemctl 作用域必须跟设置走，不能恒用 --user。

    用户级单元迁到系统级后，`systemctl --user stop <unit>` 返回 0 而什么也不做 ——
    `asip warp stop` 报成功、隧道照跑。之前只有 conf 侧探测被测试覆盖，CLI 侧
    「恒用 --user」的回归全绿逃逸。
    """
    env = _cli_env(tmp_path, ASIP_CLASH_API="http://127.0.0.1:1")
    bindir, log = _fake_bin(tmp_path, "systemctl", "echo inactive")
    # system 作用域下 CLI 走 `sudo -n systemctl`（非 root 时），必须桩住 sudo，
    # 否则真实 sudo 会真的打系统总线。桩只剥掉 -n 再转发，argv 照样进 systemctl 日志。
    sudo = bindir / "sudo"
    sudo.write_text(f'#!/bin/bash\n[ "$1" = "-n" ] && shift\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)

    def scope_args(scope):
        text = env.read_text(encoding="utf-8")
        env.write_text(re.sub(r"^ASIP_SYSTEMD_SCOPE=.*$", f"ASIP_SYSTEMD_SCOPE={scope}",
                              text, count=1, flags=re.M), encoding="utf-8")
        if log.exists():
            log.unlink()
        subprocess.run(["bash", str(PROJECT / "deploy/asip-cli.sh"), "warp", "status"],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "ASIP_CLI_ENV": str(env),
                            "PATH": f"{bindir}:{os.environ['PATH']}"})
        return log.read_text(encoding="utf-8") if log.exists() else ""

    user_args = scope_args("user")
    assert "--user" in user_args, f"user 作用域下未加 --user：{user_args[:200]}"
    system_args = scope_args("system")
    assert system_args, "system 作用域下没调用 systemctl"
    assert "--user" not in system_args, \
        f"system 作用域下仍用 --user（会静默操作不存在的单元）：{system_args[:200]}"
