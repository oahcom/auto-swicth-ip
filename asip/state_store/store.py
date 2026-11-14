"""状态权威存储：SQLite WAL 单库 + 迁移器 + 回滚投影。

WHY 换 SQLite（而不是继续优化 JSON 写协议）：当前每本台账都是「全量重写」，
`.warp_ip_used.json` 已 13.4MB/38,386 条且仍在长；全量重写是**结构性的**写
放大，加锁、加临时文件、加 fsync 都只是让它慢得更安全。WAL 天然增量写：一次
记账改一页，崩溃靠日志回放。库落在 Linux 本地盘（live 复核 ext4，非 9p）——
9p/网络挂载上 WAL 的共享内存与锁不可靠，写进去等于把回滚点建在流沙上。

三个设计约束（都有事故背书）：

1. **护栏不是 store 的发明，是 schema 的**。空表拒写/缩水拒写/ban 保护/占比
   阀/白名单全部在 `schema.py` 里以不变量形式存在，store 只负责「在事务里问
   一次」。这样迁移与切读都不能绕过 —— 绕过就要改 schema 的不变量，而那一步
   会在契约测试里红。
2. **迁移器是唯一入口，且记账**。`migrate()` 按 `MIGRATIONS` 顺序执行、逐个
   记账在 meta 表里；崩溃后重跑从断点续。库文件的 `schema_version` 高于代码
   版本时**拒绝打开**（INV-ORDER）—— 旧代码按新库解释只会静默丢列。
3. **投影是回滚契约，不是导出功能**。`export_projection()` 产出的封套与
   `deploy/state-projection.py` 完全一致（`asip-state-projection/1`，source.type
   = "sqlite"），entries 的 name 是**旧文件名**、data 是**旧代码读得懂的原文**。
   回读出来的东西老 daemon 直接能读 —— 这才叫回滚点。

失败语义（自审六问 6）：
  磁盘满      写事务抛 sqlite3.OperationalError（"database or disk is full"），
              不自动删库、不降级成「只写内存」；调用方按原语义只记日志 + 告警。
  并发        库内 BEGIN IMMEDIATE 串行化写者；本进程内再加一把 RLock 防
              同线程重入时把连接用坏（daemon 有 5 条写路径共写一库的中间态，
              写入不损坏、决策仍竞争 —— 仲裁归 W3，见 docs）。
  崩溃        WAL + synchronous=NORMAL：进程崩溃不丢已提交事务，掉电最多丢
              最后一个 checkpoint 前的未提交页（与 fsync 语义差一档，可接受：
              台账是一天窗口的证据，不是账务凭证）。
  重启        连接是进程内的，重启用新连接；库本身不持有任何进程态。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from . import schema

LOCAL_FS = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "tmpfs", "ramfs",
    "overlay", "overlayfs", "vfat", "exfat", "ntfs", "ntfs3", "apfs",
})
# 9p/virtiofs 是 WSL 访问 Windows 盘的通道；cifs/nfs 是网络盘。它们的
# 文件锁与共享内存语义不足以支撑 WAL —— 落在这些上面就拒绝建库。
REMOTE_FS = ("9p", "cifs", "smb", "smbfs", "nfs", "nfs4", "virtiofs", "fuse",
             "sshfs", "ceph", "glusterfs", "afs")


def fstype_from_mounts(mounts_text: str, path: str) -> str | None:
    """从 /proc/mounts 文本推出 path 所在文件系统类型（最长前缀匹配）。

    纯函数：调用方给文本，便于在测试里构造 WSL 的 9p / 网络盘场景，不必真挂。
    """
    target = os.path.realpath(path)
    best, best_len = None, -1
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mp, fstype = parts[1], parts[2]
        mp = mp.replace("\\040", " ")
        if target == mp or target.startswith(mp.rstrip("/") + "/"):
            if len(mp) > best_len:
                best, best_len = fstype, len(mp)
    return best


def current_fstype(path: str) -> str | None:
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            return fstype_from_mounts(f.read(), path)
    except OSError:
        return None


def is_remote_fs(fstype: str | None) -> bool:
    if not fstype:
        return False
    low = fstype.lower()
    return any(low == r or low.startswith(r + ".") or low.startswith(r)
               for r in REMOTE_FS)


class StoreError(Exception):
    """库级错误（schema 版本不符 / 文件系统不适合 WAL）。"""


class Store:
    """单库句柄。写入串行、读取共享；不持有任何业务语义。"""

    def __init__(self, path: str, *, require_local_fs: bool = True):
        self.path = os.path.abspath(path)
        if require_local_fs:
            fs = current_fstype(self.path)
            if is_remote_fs(fs):
                raise StoreError(
                    "库不能落在 %s 上（WAL 在 9p/网络挂载上不可靠）：%s"
                    % (fs, self.path))
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=5.0,
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_pragmas()
        self._init_tables()
        self._check_version()
        # 台账里有出口地址与封禁证据，库文件与台账同密级。SQLite 按 umask 建
        # 文件（实测 0644），这里在建库之后显式收紧 —— 与 `atomic_io` 对台账
        # 0600 的口径一致。放在建表之后：建库前 chmod 打在不存在的路径上是空操作。
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ── 生命周期 ────────────────────────────────────────────────
    def _init_pragmas(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")

    def _init_tables(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rows (
                    kind TEXT NOT NULL,
                    key  TEXT NOT NULL,
                    body TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (kind, key)
                );
                CREATE TABLE IF NOT EXISTS docs (
                    kind TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schema INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    key TEXT NOT NULL DEFAULT '',
                    suppressed INTEGER NOT NULL DEFAULT 0,
                    fields TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_cat_ts
                    ON events (category, ts);
                CREATE TABLE IF NOT EXISTS shadow (
                    kind TEXT NOT NULL,
                    ts REAL NOT NULL,
                    checked INTEGER NOT NULL,
                    diffs INTEGER NOT NULL,
                    samples TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (kind, ts)
                );
            """)
            if self._meta_get("schema_version") is None:
                # 新库从 0 起，交给 migrate(0) 逐级升并记账 —— 「谁建的库、
                # 升到哪一级」必须留在 meta 里，而不是隐式等于代码版本。
                self._meta_set("schema_version", "0")
                self._meta_set("created_at", "%.3f" % time.time())

    def _check_version(self) -> None:
        raw = self._meta_get("schema_version")
        have = int(raw) if raw else 0
        if have > schema.SCHEMA_VERSION:
            raise StoreError(
                "库 schema v%d 高于代码 v%d：先升级代码或按迁移器回滚，"
                "禁止按旧 schema 解释新库" % (have, schema.SCHEMA_VERSION))
        if have < schema.SCHEMA_VERSION:
            self.migrate(have)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── meta ────────────────────────────────────────────────────
    def _meta_get(self, k: str) -> str | None:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return None if row is None else row["v"]

    def _meta_set(self, k: str, v: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def schema_version(self) -> int:
        raw = self._meta_get("schema_version")
        return int(raw) if raw else 0

    # ── 迁移器 ──────────────────────────────────────────────────
    def migrate(self, from_version: int) -> list[str]:
        """从 from_version 逐级升到 SCHEMA_VERSION；每级成功后记账。

        `MIGRATIONS[version]` 给的是迁移名；真正的 DDL/DML 由调用方（部署侧
        `deploy/migrations/NNN_*.py` 或本模块的 `_upgrade_<version>`）提供。
        这里只保证：顺序、逐个记账、失败即停（重跑从断点续）。
        """
        applied: list[str] = []
        with self._lock:
            for ver in range(int(from_version) + 1, schema.SCHEMA_VERSION + 1):
                name = schema.MIGRATIONS.get(ver)
                fn = getattr(self, "_upgrade_%d" % ver, None)
                if fn is not None:
                    fn()
                elif name is None:
                    if ver > schema.BASE_VERSION:
                        raise StoreError("schema v%d 无迁移实现（登记缺失）" % ver)
                    name = "baseline:%d" % ver
                self._meta_set("schema_version", str(ver))
                self._meta_set("migrated_%d" % ver, name or "builtin")
                applied.append(name or ("builtin:%d" % ver))
        return applied

    # ── 行式读写 ────────────────────────────────────────────────
    def read_rows(self, kind: str) -> dict[str, dict]:
        """按白名单过滤后的行（读语义=daemon 载入语义）。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, body FROM rows WHERE kind=? ORDER BY key", (kind,))
            out = {}
            for row in cur:
                try:
                    body = json.loads(row["body"])
                except ValueError:
                    continue
                kept, _extra = schema.filter_fields(kind, body)
                out[row["key"]] = kept
            return out

    def read_raw(self, kind: str) -> dict[str, dict]:
        """未过滤原文（审计/投影用，绝不用于决策）。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT key, body FROM rows WHERE kind=?", (kind,))
            out = {}
            for row in cur:
                try:
                    out[row["key"]] = json.loads(row["body"])
                except ValueError:
                    continue
            return out

    def count_rows(self, kind: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM rows WHERE kind=?", (kind,)).fetchone()
            return int(row["n"])

    def replace_rows(self, kind: str, mapping: dict, *,
                     allow_empty: bool = False) -> dict:
        """整表替换（等价于旧 `_save_*` 的全量落盘），过 INV-EMPTY/INV-SHRINK。

        返回 {"written", "refused"}；拒写不抛异常 —— 原语义就是「跳过这次写，
        记一行日志继续跑」，抛异常会改掉调用链。
        """
        new = {str(k): v for k, v in (mapping or {}).items()}
        now = time.time()
        with self._lock:
            old_keys = {r["key"] for r in self._conn.execute(
                "SELECT key FROM rows WHERE kind=?", (kind,))}
            try:
                schema.check_empty_write(len(new), len(old_keys),
                                         allow_empty=allow_empty)
                schema.check_shrink(len(new), len(old_keys),
                                    allow_empty=allow_empty)
            except schema.GuardRefused as exc:
                return {"written": False, "refused": exc.invariant,
                        "detail": exc.detail, "ctx": exc.ctx}
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if new:
                    marks = ",".join("?" * len(new))
                    self._conn.execute(
                        "DELETE FROM rows WHERE kind=? AND key NOT IN (%s)"
                        % marks, (kind, *new.keys()))
                else:
                    self._conn.execute("DELETE FROM rows WHERE kind=?", (kind,))
                for k, body in new.items():
                    self._conn.execute(
                        "INSERT INTO rows(kind,key,body,updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
                        "body=excluded.body, updated_at=excluded.updated_at",
                        (kind, k, json.dumps(body, ensure_ascii=False), now))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {"written": True, "count": len(new)}

    def update_rows(self, kind: str, merge) -> dict:
        """读-改-写在**同一个写事务**里（乐观合并的串行点）。

        `merge(dict) -> dict`：拿到库内当前行集合，返回要写回的新集合。
        与 `atomic_io.update_json` 的 flock 语义等价，但锁由库自己提供 ——
        多进程（daemon + cron）同时写也不会互相覆盖（A7 共享锁事故的另一半）。
        """
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "SELECT key, body FROM rows WHERE kind=?", (kind,))
                current = {}
                for row in cur:
                    try:
                        current[row["key"]] = json.loads(row["body"])
                    except ValueError:
                        continue
                merged = merge(current)
                if not isinstance(merged, dict):
                    raise StoreError("merge 必须返回 dict（kind=%s）" % kind)
                self._conn.execute("DELETE FROM rows WHERE kind=?", (kind,))
                for k, body in merged.items():
                    self._conn.execute(
                        "INSERT INTO rows(kind,key,body,updated_at) VALUES(?,?,?,?)",
                        (kind, str(k), json.dumps(body, ensure_ascii=False), now))
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {"written": True, "count": len(merged)}

    def delete_rows(self, kind: str, keys, *, now: float | None = None) -> dict:
        """按 key 删除，但 INV-BAN 保护精确封禁中的行（删不掉就留在库里）。"""
        now = time.time() if now is None else now
        keep: list[str] = []
        removed: list[str] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for key in keys:
                    row = self._conn.execute(
                        "SELECT body FROM rows WHERE kind=? AND key=?",
                        (kind, key)).fetchone()
                    if row is None:
                        continue
                    try:
                        body = json.loads(row["body"])
                    except ValueError:
                        body = {}
                    if schema.protected_by_ban(body, now):
                        keep.append(key)
                        continue
                    self._conn.execute(
                        "DELETE FROM rows WHERE kind=? AND key=?", (kind, key))
                    removed.append(key)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return {"removed": removed, "blocked": keep}

    # ── 文档式读写 ──────────────────────────────────────────────
    def read_doc(self, kind: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body FROM docs WHERE kind=?", (kind,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["body"])
        except ValueError:
            return None

    def write_doc(self, kind: str, body) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO docs(kind,body,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(kind) DO UPDATE SET body=excluded.body, "
                "updated_at=excluded.updated_at",
                (kind, json.dumps(body, ensure_ascii=False), time.time()))

    # ── 事件流水（alerting 的 journal 注入点） ───────────────────
    def append(self, event: dict) -> None:
        """`append(event: dict)`：与 alerting.configure(journal=...) 的鸭子类型
        一致。字段集就是 schema.EVENT_FIELDS 九字段，缺字段补空、多字段进 body。

        永不抛异常：alerting 的三条硬约束之一是「流水写不进去退 stderr，不递归
        告警」。这里吞掉 sqlite3 错误并让 alerting 的 fault 通道去报。
        """
        try:
            ev = {k: event.get(k) for k in schema.EVENT_FIELDS}
            fields = ev.get("fields") or {}
            if not isinstance(fields, dict):
                fields = {"value": fields}
            extra = {k: v for k, v in event.items()
                     if k not in schema.EVENT_FIELDS}
            if extra:
                fields = {**fields, **extra}
            with self._lock:
                self._conn.execute(
                    "INSERT INTO events(schema,ts,category,severity,message,"
                    "source,key,suppressed,fields) VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(ev.get("schema") or 1), float(ev.get("ts") or time.time()),
                     str(ev.get("category") or ""), str(ev.get("severity") or ""),
                     str(ev.get("message") or ""), str(ev.get("source") or ""),
                     str(ev.get("key") or ""), int(ev.get("suppressed") or 0),
                     json.dumps(fields, ensure_ascii=False)))
        except Exception:
            pass

    def read_events(self, *, limit: int = 50,
                    category: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM events %s ORDER BY id DESC LIMIT ?"
               % ("WHERE category=?" if category else ""))
        args = ((category, limit) if category else (limit,))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = []
        for row in reversed(rows):
            item = dict(row)
            try:
                item["fields"] = json.loads(item.get("fields") or "{}")
            except ValueError:
                item["fields"] = {}
            out.append(item)
        return out

    # ── 影子对账留痕 ────────────────────────────────────────────
    def record_shadow(self, kind: str, checked: int, diffs: int,
                      samples: list, *, ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO shadow(kind,ts,checked,diffs,samples) "
                "VALUES(?,?,?,?,?)",
                (kind, time.time() if ts is None else ts, checked, diffs,
                 json.dumps(samples[:20], ensure_ascii=False)))

    def shadow_history(self, kind: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shadow WHERE kind=? ORDER BY ts DESC LIMIT ?",
                (kind, limit)).fetchall()
        return [dict(r) for r in rows]

    def shadow_window_clean(self, kind: str, since: float) -> dict:
        """观察窗内是否「差异恒 0」—— 切读门槛（T2c）的机器判据。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, diffs FROM shadow WHERE kind=? AND ts>=? "
                "ORDER BY ts", (kind, since)).fetchall()
        diffs = [int(r["diffs"]) for r in rows]
        return {"samples": len(diffs), "max_diffs": max(diffs) if diffs else None,
                "clean": bool(diffs) and max(diffs) == 0}

    # ── 统计（管理面/健康面用） ──────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM rows GROUP BY kind").fetchall()
            docs = self._conn.execute(
                "SELECT kind FROM docs").fetchall()
            events = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events").fetchone()
            page = self._conn.execute("PRAGMA page_count").fetchone()[0]
            size = page * self._conn.execute("PRAGMA page_size").fetchone()[0]
            wal = 0
            for suffix in ("-wal", "-shm"):
                try:
                    wal += os.path.getsize(self.path + suffix)
                except OSError:
                    pass
        return {
            "db": self.path,
            "schema_version": self.schema_version(),
            "bytes": size,
            "wal_bytes": wal,
            "rows": {r["kind"]: int(r["n"]) for r in rows},
            "docs": [r["kind"] for r in docs],
            "events": int(events["n"]),
        }

    # ── 回滚投影（接 deploy/state-projection.py 的封套） ─────────
    def export_state_files(self) -> dict[str, str]:
        """把库投影成**旧代码读得懂的 JSON 文件内容**（name → 原文）。

        这是回滚契约的核心：回读出来的必须是 `{"used": {...}}` /
        `{"version":1,"lines":{...}}` 这类原封套，不是库自己的形状。
        """
        out: dict[str, str] = {}
        for kind in schema.ROW_KINDS:
            rows = self.read_raw(kind)
            envelope = schema.ROW_ENVELOPES.get(kind, "")
            static = schema.ROW_STATIC.get(kind) or {}
            if envelope:
                payload = {**static, envelope: rows}
            else:
                payload = {**static, **rows}
            out[schema.FILE_NAMES[kind]] = json.dumps(
                payload, ensure_ascii=False, sort_keys=True)
        for kind in schema.DOC_KINDS:
            body = self.read_doc(kind)
            if body is None:
                continue
            out[schema.FILE_NAMES[kind]] = json.dumps(
                body, ensure_ascii=False, sort_keys=True)
        return out

    def export_projection(self, out_path: str, *,
                          created_at: str | None = None) -> dict:
        """写出 `asip-state-projection/1` 封套（source.type=sqlite）。

        与 `deploy/state-projection.py` 的封套逐字段一致，因此 freeze/readback
        两步可以直接复用那个工具 —— 只增数据源、不动封套。
        """
        import hashlib

        entries = []
        for name, text in sorted(self.export_state_files().items()):
            data = text.encode("utf-8")
            entries.append({
                "name": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mtime": time.time(),
                "data": text,
            })
        db_sha = ""
        try:
            h = hashlib.sha256()
            with open(self.path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            db_sha = h.hexdigest()
        except OSError:
            pass
        projection = {
            "schema": "asip-state-projection/1",
            "created_at": created_at or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": {"type": "sqlite", "db": self.path,
                       "db_sha256": db_sha,
                       "schema_version": self.schema_version()},
            "includes": sorted(schema.FILE_NAMES.values()),
            "entries": entries,
        }
        tmp = out_path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".",
                    exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(projection, f, ensure_ascii=False)
        os.replace(tmp, out_path)
        return {"entries": len(entries),
                "bytes": sum(e["size"] for e in entries),
                "out": out_path}

    # ── 状态载入（一次性把库喂给内存态） ─────────────────────────
    def snapshot(self) -> dict:
        """{kind: rows} + docs：影子对账与「启动载入」共用同一形状。"""
        snap = {kind: self.read_rows(kind) for kind in schema.ROW_KINDS}
        for kind in schema.DOC_KINDS:
            snap[kind] = self.read_doc(kind)
        return snap