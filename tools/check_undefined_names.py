#!/usr/bin/env python3
"""静态名称检查：抓出「作用域里不存在」的名字。

WHY 需要它：`ast.parse()` 只查语法，**不查名字**。`PROJECT` 写成 `_PROJECT`
是语法完全合法的 Python，但运行时才抛 NameError —— 而 daemon 是被 systemd
`Restart=always` 托管的，NameError 会变成「启动 → 崩溃 → 重启 → 崩溃」的
重启风暴（2026-11-11 实测：4 次重启才起来，每次都带一堆 sing-box 残留
进程告警）。所以部署前必须有一道**不启动进程**就能发现未定义名字的关卡。

**为什么用 symtable 而不是自己走 AST**：自己区分「模块级 / 函数级 / 类级」
作用域很容易错（我第一版就只扫模块顶层，结果函数体内的 `PROJECT` 根本
没进差集 —— 工具报「全部通过」，而运行时照样炸）。`symtable` 是 Python
标准库自带的**编译器用的同一套作用域分析**：它给出每个作用域的自由变量
（free variables）与全局变量（globals），未定义的名字就在其中。

用法：python3 tools/check_undefined_names.py <文件...>
"""
from __future__ import annotations

import builtins
import symtable
import sys
from pathlib import Path

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", 
                           "__package__", "__spec__", "__loader__"}


def _collect(table: symtable.SymbolTable, path: str,
             out: list[tuple[str, str]]) -> None:
    """递归一个作用域表，把「自由变量里既不是全局定义、也不是内置」的记下来。

    symtable 给出三种引用：
      - `is_global()`  ：这个作用域里读的名字，在**模块级**查找
      - `is_free()`    ：这个作用域里读的名字，在**外层函数作用域**查找
      - 都不满足        ：局部变量

    模块级的 `PROJECT` 若是真未定义，那么在**任何**引用它的作用域里，
    它都会以 `is_global()` 出现，且模块级符号表里没有它 → 这就是要报的。
    """
    for sym in table.get_symbols():
        if not (sym.is_global() or sym.is_free()):
            continue
        if sym.get_name() in BUILTINS:
            continue
        out.append((path, sym.get_name()))


def check_file(path: Path) -> list[tuple[str, str]]:
    try:
        src = path.read_text(encoding="utf-8")
    except OSError as e:
        return [(f"<file>: {e}", "")]
    try:
        top = symtable.symtable(src, str(path), "exec")
    except SyntaxError as e:
        return [(f"第 {e.lineno or 0} 行: {e.msg}", "")]

    # 模块级真正定义了什么
    module_defined = {s.get_name() for s in top.get_symbols()
                      if s.is_assigned() or s.is_imported()
                      or s.is_namespace()}

    missing: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def walk(table: symtable.SymbolTable, prefix: str,
             outer_defined: set[str]) -> None:
        # 本作用域自己定义的名字（参数、局部赋值、import…）
        local = {s.get_name() for s in table.get_symbols()
                 if s.is_assigned() or s.is_imported() or s.is_parameter()
                 or s.is_namespace()}
        for sym in table.get_symbols():
            name = sym.get_name()
            if name in BUILTINS or name in module_defined or name in local:
                continue
            if name in outer_defined:
                continue
            if sym.is_global() or sym.is_free():
                # 全局引用但模块级没定义 → 未定义
                key = (prefix + name, name)
                if key not in seen:
                    seen.add(key)
                    missing.append(key)
        child_defined = set(local)
        for child in table.get_children():
            walk(child, prefix + child.get_name() + ".", child_defined)

    walk(top, "", set())
    return missing


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]]
    if not files:
        print("用法: python3 tools/check_undefined_names.py <文件...>", file=sys.stderr)
        return 2
    bad = 0
    for f in files:
        issues = check_file(f)
        if issues:
            bad += len(issues)
            print(f"✗ {f}")
            for where, name in issues[:20]:
                print(f"    {where}: 未定义的名字 {name}")
            if len(issues) > 20:
                print(f"    …另有 {len(issues)-20} 处")
        else:
            print(f"✓ {f}")
    if bad:
        print(f"\n共 {bad} 处未定义名字")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
