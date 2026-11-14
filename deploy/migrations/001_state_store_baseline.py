"""状态库基线（schema v1）：建库 + 建表 + 写 meta（幂等）。

登记在 `deploy/asip-release.py migrate` 的台账里：一次 schema 迁移 = 一个
migration 文件。本文件是**基线**（表结构即 v1），`up()` 幂等 —— 已建过的库
重跑无副作用；`down()` **故意不删库**（INV-UNMIGRATED：数据比结构重要，
结构回退用下一个版本的迁移，删库不是回滚）。

为什么必须走迁移器而不是「daemon 启动时自己建库」：daemon 启动建库会让
「库的 schema 版本」变成隐式的（谁先启动谁定），多开发者 + 多机部署下就是
又一次「两个真相源」。迁移器是显式、有序、可记账的入口。
"""
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def _store():
    if PROJECT not in sys.path:
        sys.path.insert(0, PROJECT)
    from config import CFG
    from asip.state_store import Store
    return Store(CFG["state_store"]["db_path"])


def up() -> None:
    st = _store()
    try:
        print("  状态库就绪：%s（schema v%d）"
              % (st.path, st.schema_version()))
    finally:
        st.close()


def down() -> None:
    # 结构回退不删数据：库文件保留，由后续迁移显式处理。
    print("  基线迁移不可回退（数据保留）")