"""告警窄接口：事件流水 + 可插拔 AlertSink + 故障隔离。

WHY（4a 的存在理由）：现状一切只进 daemon.log（5MB×5 轮转），滚掉即无因果；
写台账失败只 log 一行就继续跑，磁盘满时连这一行也会被自己吞掉 —— 脏地址
随后被当作干净地址复用。本模块给「已知的坏」一条独立出口，且保证告警自身
故障不得反向影响被观测的业务路径。

必报清单（4a 定稿；检测器在 4b/P3 接线，类目先固化）：

  - ``thread_dead``              线程死亡（4b 监督树探活后接线）
  - ``state_write_failed``       状态/台账写盘失败（本波已接线到
                                 ``daemon._write_json_atomic``）
  - ``ledger_anomaly``           台账异常（空表拒写/条数缩水拒写/坏文件；
                                 本波已接线「缩水拒写」这一处）
  - ``stale_evidence_consumed``  读路径消费过期证据（4b 新鲜度契约后接线）
  - ``observation_drift``        观测口径漂移（SEEN vs USED、产物/生效/台账
                                 三面不一致；比较逻辑在 asip.config_projection）

三条硬约束：

1. ``emit()`` 永不抛异常。调用点在业务路径的异常分支上（写盘失败），
   再抛一次会改掉原语义 —— 台账写失败本来就不该掀翻调用链。
2. 事件先落流水再外发。流水写不进去时退 stderr，**不递归告警**（否则磁盘满
   会变成告警风暴，把最后一 MB 也写掉）。
3. 节流按 key，窗口内只发一条；下一条带 ``suppressed`` 计数。写盘失败是
   「每台账每次写」都触发，不节流会把流水自己写爆。

P3 对接形态：``configure(journal=...)`` 接受任何带 ``append(event: dict)``
的对象。JSONL 只是过渡实现；状态权威落 SQLite 后传入事件表 writer 即可，
本模块与调用点都不用改。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from typing import Any, Callable, Mapping, Protocol

from ..atomic_io import append_line

SCHEMA_VERSION = 1

# 必报类目（字符串稳定：消费方按它查询，不按 message 文案）
CATEGORY_THREAD_DEAD = "thread_dead"
CATEGORY_STATE_WRITE_FAILED = "state_write_failed"
CATEGORY_LEDGER_ANOMALY = "ledger_anomaly"
CATEGORY_STALE_EVIDENCE = "stale_evidence_consumed"
CATEGORY_OBSERVATION_DRIFT = "observation_drift"

KNOWN_CATEGORIES = frozenset({
    CATEGORY_THREAD_DEAD,
    CATEGORY_STATE_WRITE_FAILED,
    CATEGORY_LEDGER_ANOMALY,
    CATEGORY_STALE_EVIDENCE,
    CATEGORY_OBSERVATION_DRIFT,
})

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"


class AlertSink(Protocol):
    """外发通道窄接口：一个名字 + 一次投递。

    只收「已定型的事件 dict」，不参与节流/落盘/去重 —— 那些是 Dispatcher 的
    职责。通道实现只需保证「失败就抛」，隔离由 Dispatcher 统一兜（见
    ``Dispatcher.emit``）。邮件/IM 各写一个类即可，不改调用点。
    """

    name: str

    def emit(self, event: dict) -> None: ...


class FunctionSink:
    """把任意可调用对象包成 sink（mock 演示/临时通道/测试用）。"""

    def __init__(self, name: str, fn: Callable[[dict], None]):
        self.name = name
        self._fn = fn

    def emit(self, event: dict) -> None:
        self._fn(event)


class WebhookSink:
    """HTTP POST 参考实现；超时短、失败就抛，由 Dispatcher 记通道故障。

    不在这里吞异常：吞了就没法区分「通道坏了」和「事件发出去了」。
    """

    def __init__(self, url: str, *, timeout: float = 2.0, name: str = "webhook"):
        self.name = name
        self.url = url
        self.timeout = timeout

    def emit(self, event: dict) -> None:
        body = json.dumps(event, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "asip-alert/%d" % SCHEMA_VERSION})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read(1)


class EventJournal:
    """JSONL 事件流水（append-only，逐行 fsync）。

    为什么单独一份文件：daemon.log 是 5MB×5 轮转，告警恰恰在磁盘满/风暴时
    最先被滚掉。JSONL 每行一条完整事件，消费方（P3 事件表、审计 CLI）可按行
    解析，滚失风险从「5 个文件」降到「一次显式轮转」。

    轮转只留一代（``<path>.1``）：过渡实现，P3 事件表落地后由表保留策略接管。
    """

    def __init__(self, path: str, *, max_bytes: int = 16 * 1024 * 1024):
        self.path = os.path.abspath(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            # 轮转失败不能挡住写：继续追加，让体积问题暴露在通道故障里。
            pass

    def append(self, event: dict) -> None:
        with self._lock:
            self._rotate_if_needed()
            append_line(self.path, event)

    def read_recent(self, limit: int = 50) -> list[dict]:
        """排障用：读尾部 N 条（坏行跳过，绝不因一行坏 JSON 让整份不可读）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out: list[dict] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _jsonable(value: Any) -> Any:
    """把字段压成可 JSON 序列化的形态：事件落盘失败等于没告警。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


class _ChannelFault:
    """通道/流水自身故障的 stderr 出口（限流，且绝不反向 emit）。"""

    def __init__(self, window: float = 60.0):
        self._window = window
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def report(self, where: str, exc: BaseException) -> None:
        now = time.time()
        with self._lock:
            if now - self._last.get(where, 0.0) < self._window:
                return
            self._last[where] = now
            print("⚠ asip 告警通道故障（%s）: %s: %s"
                  % (where, type(exc).__name__, exc), file=sys.stderr)
            sys.stderr.flush()


class Dispatcher:
    """节流 + 落流水 + 多通道扇出；任何单点故障都在这里被吃掉。"""

    def __init__(self, journal, *, throttle_sec: float = 60.0,
                 clock: Callable[[], float] = time.time):
        self._journal = journal
        self._throttle_sec = float(throttle_sec)
        self._clock = clock
        self._sinks: list[AlertSink] = []
        self._lock = threading.Lock()
        # key → [上次放行时刻, 窗口内被压掉的条数]
        self._windows: dict[str, list[float]] = {}
        self._faults = _ChannelFault()

    # ── 通道插拔 ────────────────────────────────────────────────
    def add_sink(self, sink: AlertSink) -> None:
        with self._lock:
            if any(s.name == sink.name for s in self._sinks):
                raise ValueError("sink 重名: %s" % sink.name)
            self._sinks.append(sink)

    def remove_sink(self, name: str) -> bool:
        with self._lock:
            before = len(self._sinks)
            self._sinks = [s for s in self._sinks if s.name != name]
            return len(self._sinks) != before

    @property
    def sinks(self) -> tuple[AlertSink, ...]:
        with self._lock:
            return tuple(self._sinks)

    # ── 投递 ────────────────────────────────────────────────────
    def emit(self, category: str, message: str, *,
             severity: str = SEVERITY_WARNING,
             key: str | None = None,
             source: str = "",
             **fields: Any) -> bool:
        """返回是否真的放行了一条事件（被节流压掉 = False）。

        顺序：先判节流（窗口内只累积 suppressed），再落流水，再扇出。
        落流水失败不阻断扇出（日志通道可能还活着）；通道失败不阻断其它通道，
        也不向调用方抛。
        """
        now = self._clock()
        dedupe_key = key or "%s|%s" % (category, source or "-")
        with self._lock:
            window = self._windows.get(dedupe_key)
            if window is not None and now - window[0] < self._throttle_sec:
                window[1] += 1
                return False
            suppressed = int(window[1]) if window else 0
            self._windows[dedupe_key] = [now, 0.0]
            sinks = tuple(self._sinks)

        event = {
            "schema": SCHEMA_VERSION,
            "ts": round(now, 3),
            "category": category,
            "severity": severity,
            "message": message,
            "source": source,
            "key": dedupe_key,
            "suppressed": suppressed,
            "fields": _jsonable(fields),
        }

        try:
            self._journal.append(event)
        except Exception as exc:      # 流水写失败：退 stderr，继续扇出
            self._faults.report("event_journal", exc)

        for sink in sinks:
            try:
                sink.emit(event)
            except Exception as exc:  # 单个通道坏掉不拖累其它通道
                self._faults.report("sink:%s" % sink.name, exc)
        return True


# ── 进程级默认实例（facade） ────────────────────────────────────
# WHY 允许这一个模块级单例：告警通道天然是「进程一条」，而调用点在
# daemon 的深层异常分支上，不能要求把 dispatcher 逐层透传。它是**可替换的
# 注入点**（configure/reset），不是跨模块私有状态：外部只经 emit/add_sink 使用。
_default_lock = threading.Lock()
_default: Dispatcher | None = None


def configure(*, journal_path: str | None = None, journal=None,
              log_sink: Callable[[str], None] | None = None,
              webhook_url: str | None = None,
              throttle_sec: float = 60.0,
              sinks: tuple[AlertSink, ...] = ()) -> Dispatcher:
    """建立进程默认 dispatcher；重复调用=整体替换（测试与热重配都用它）。"""
    global _default
    if journal is None:
        if not journal_path:
            raise ValueError("configure 需要 journal_path 或 journal")
        journal = EventJournal(journal_path)
    disp = Dispatcher(journal, throttle_sec=throttle_sec)
    if log_sink is not None:
        disp.add_sink(FunctionSink("log", lambda e: log_sink(_format_for_log(e))))
    if webhook_url:
        disp.add_sink(WebhookSink(webhook_url))
    for sink in sinks:
        disp.add_sink(sink)
    with _default_lock:
        _default = disp
    return disp


def reset() -> None:
    """摘掉默认 dispatcher（测试隔离 / 回滚点：钩子随即变成 no-op）。"""
    global _default
    with _default_lock:
        _default = None


def get_default() -> Dispatcher | None:
    with _default_lock:
        return _default


def add_sink(sink: AlertSink) -> None:
    disp = get_default()
    if disp is None:
        raise RuntimeError("alerting 未配置：先 configure()")
    disp.add_sink(sink)


def remove_sink(name: str) -> bool:
    disp = get_default()
    return bool(disp and disp.remove_sink(name))


def emit(category: str, message: str, **kwargs: Any) -> bool:
    """未配置时静默 no-op：pytest / 手工 import 不落任何生产文件。"""
    disp = get_default()
    if disp is None:
        return False
    return disp.emit(category, message, **kwargs)


def notify_write_failure(path: str, exc: BaseException, *,
                         source: str = "") -> bool:
    """写盘失败升级入口（daemon._write_json_atomic 的异常分支调用）。

    节流键带完整路径：磁盘满时每条台账都失败，按 path 分键才能看出「哪些
    台账已经停更」，而不是被一条汇总消息掩盖。
    """
    return emit(
        CATEGORY_STATE_WRITE_FAILED,
        "写 %s 失败: %s" % (os.path.basename(path), exc),
        severity=SEVERITY_ERROR,
        key="write_failed:%s" % os.path.abspath(path),
        source=source,
        path=os.path.abspath(path),
        error="%s: %s" % (type(exc).__name__, exc),
    )


def _format_for_log(event: Mapping[str, Any]) -> str:
    extra = ""
    if event.get("suppressed"):
        extra = "（窗口内又压掉 %d 条）" % event["suppressed"]
    return "⚠ 告警[%s/%s] %s%s source=%s" % (
        event.get("severity"), event.get("category"), event.get("message"),
        extra, event.get("source") or "-")


__all__ = [
    "AlertSink", "Dispatcher", "EventJournal", "FunctionSink", "WebhookSink",
    "KNOWN_CATEGORIES",
    "CATEGORY_THREAD_DEAD", "CATEGORY_STATE_WRITE_FAILED",
    "CATEGORY_LEDGER_ANOMALY", "CATEGORY_STALE_EVIDENCE",
    "CATEGORY_OBSERVATION_DRIFT",
    "SEVERITY_INFO", "SEVERITY_WARNING", "SEVERITY_ERROR", "SEVERITY_CRITICAL",
    "configure", "reset", "get_default", "add_sink", "remove_sink", "emit",
    "notify_write_failure",
]
