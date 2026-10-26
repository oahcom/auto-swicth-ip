"""config 与 daemon 的导入契约：daemon 用到的名字必须在提交过的 config 里。"""
import ast
from pathlib import Path

import config as cfg_mod


def test_daemon_config_imports_exist():
    """daemon.py 从 config 导入的每个名字都必须真实存在。

    2026-10-27 踩过：daemon 里加了 `TRANSPORT_LOOKBACK_SEC`，config 的这行却
    留在未提交的工作区 —— 生产能跑（脏文件在），干净 clone 直接 ImportError
    起不来。这类「只提交了一半」靠人眼看不出来，交给测试。
    """
    tree = ast.parse((Path(__file__).parent / "daemon.py").read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "config":
            names += [a.name for a in node.names]
    assert names, "daemon.py 未从 config 导入任何名字，解析可能已失效"
    missing = [n for n in names if not hasattr(cfg_mod, n)]
    assert missing == [], f"config 缺这些导出（未提交的 config 改动？）: {missing}"
