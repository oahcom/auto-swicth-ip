"""刷新落盘时旁路文件（.stats.json / .sources.json）的搬运。

`os.replace(tmp, final)` 只搬配置本身。不搬旁路的话 `singbox-config.json
.sources.json` 永远停在 `.tmp` 版本上，渠道报表读不到订阅源归属。
"""
import ast
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
NAMES = ("_SIDECAR_SUFFIXES", "_drop_stats", "_promote_sidecars")


def _daemon():
    """只取这三个定义，不 import 整个 daemon（会拉起线程/全局状态）。

    用 AST 抽节点再编译，不靠正则抠源码 —— 正则一遇到缩进/空行变化就静默
    抓不到，用例于是「通过」一个空模块。
    """
    sys.path.insert(0, str(PROJECT))
    import os
    tree = ast.parse((PROJECT / "daemon.py").read_text())
    picked = [n for n in tree.body
              if getattr(n, "name", None) in NAMES
              or any(getattr(t, "id", None) in NAMES
                     for t in getattr(n, "targets", ()))]
    assert len(picked) == len(NAMES), [getattr(n, "name", "?") for n in picked]
    box: dict = {"os": os}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "daemon.py", "exec"),
         box)
    return box


def test_sidecars_are_moved_next_to_the_promoted_config(tmp_path):
    box = _daemon()
    tmp_cfg = tmp_path / "singbox-config.json.tmp"
    fin_cfg = tmp_path / "singbox-config.json"
    for suffix, body in ((".stats.json", '{"n": 1}'),
                         (".sources.json", '{"h:443-any": "air"}')):
        (tmp_path / f"singbox-config.json.tmp{suffix}").write_text(body)

    box["_promote_sidecars"](str(tmp_cfg), str(fin_cfg))
    assert (tmp_path / "singbox-config.json.stats.json").read_text() == '{"n": 1}'
    assert (tmp_path / "singbox-config.json.sources.json").read_text() == \
        '{"h:443-any": "air"}'
    # 搬走之后 .tmp 侧不留残骸（尤其 sources：里面有订阅归属）
    assert not list(tmp_path.glob("*.tmp.*.json"))


def test_drop_stats_cleans_both_versions(tmp_path):
    box = _daemon()
    for name in ("c.json.stats.json", "c.json.stats.json.tmp",
                 "c.json.sources.json"):
        (tmp_path / name).write_text("x")
    box["_drop_stats"](str(tmp_path / "c.json"))
    assert not list(tmp_path.iterdir())


def test_missing_sidecar_removes_the_stale_one(tmp_path):
    """这一轮没生成某份旁路 → 上一轮那份必须删掉。

    留着会让下游把旧归属当成当前配置的归属：sources 里 tag 相同、机器可能
    已经换过，报表会把「A 机场的节点名」挂到 B 机场的节点上。
    """
    box = _daemon()
    tmp_cfg = tmp_path / "singbox-config.json.tmp"
    fin_cfg = tmp_path / "singbox-config.json"
    (tmp_path / "singbox-config.json.sources.json").write_text('{"old": 1}')
    (tmp_path / "singbox-config.json.tmp.stats.json").write_text('{"n": 1}')

    box["_promote_sidecars"](str(tmp_cfg), str(fin_cfg))
    assert not (tmp_path / "singbox-config.json.sources.json").exists()
    assert (tmp_path / "singbox-config.json.stats.json").read_text() == '{"n": 1}'
