"""可观测性单元测试（Phase Two · 切片 D）。

覆盖 :mod:`scanner.observability` 的三件能力：

- :class:`JsonLogFormatter` —— 把日志记录格式化为单行、可被 ``json.loads`` 解析的
  JSON，含 ``ts/level/logger/message``，带 ``exc_info`` 时含异常文本字段。
- :func:`configure_json_logging` —— 将根日志切换为 JSON 输出，且幂等（重复调用
  不会累积 handler）。本文件用 fixture 在测试后还原全局 logging 状态，避免污染
  其它测试。
- :class:`PipelineMetrics` —— 累计量单调累加、``last_*`` 反映最近一次周期、
  ``last_cycle_at`` 取注入 clock；``snapshot()`` 可 JSON 序列化、不含 ``clock``
  键、``last_cycle_at`` 为 isoformat 字符串（未记录时为 None）。

时间用确定性的 ``FakeClock`` 注入（参考 tests/test_ingestion.py），使断言可复现。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from scanner.observability import (
    JsonLogFormatter,
    PipelineMetrics,
    configure_json_logging,
)

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """确定性、可前进的 UTC 时钟（供注入）。"""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture
def restore_root_logging():
    """保存并在测试后还原根 logger 的 handlers 与 level。

    ``configure_json_logging`` 会改写全局根 logger，这个 fixture 确保相关测试
    不污染其它测试的 logging 状态。
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        # 还原：移除测试期间新增的 handler，恢复原始 handler 列表与 level。
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# --------------------------------------------------------------------------- #
# JsonLogFormatter
# --------------------------------------------------------------------------- #


def _make_record(
    *,
    name: str = "scanner.test",
    level: int = logging.INFO,
    msg: str = "hello world",
    args=None,
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args or (),
        exc_info=exc_info,
    )


def test_formatter_outputs_valid_json_with_expected_fields():
    # format() 应输出可被 json.loads 解析的单行 JSON，含约定字段。
    formatter = JsonLogFormatter()
    record = _make_record(name="scanner.demo", level=logging.WARNING, msg="hi")

    output = formatter.format(record)

    # 单行：不应包含换行符。
    assert "\n" not in output
    payload = json.loads(output)
    assert set(["ts", "level", "logger", "message"]).issubset(payload)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "scanner.demo"
    assert payload["message"] == "hi"


def test_formatter_renders_message_with_args():
    # message 应是 record.getMessage() 的结果（%-格式化后的最终文本）。
    formatter = JsonLogFormatter()
    record = _make_record(msg="value=%s", args=("42",))

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "value=42"


def test_formatter_includes_exc_info_when_present():
    # 带 exc_info 时输出应含 exc_info 字段，且包含异常文本。
    formatter = JsonLogFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record(level=logging.ERROR, msg="failed", exc_info=sys.exc_info())

    payload = json.loads(formatter.format(record))

    assert "exc_info" in payload
    assert "ValueError" in payload["exc_info"]
    assert "boom" in payload["exc_info"]


def test_formatter_omits_exc_info_when_absent():
    # 普通记录不应含 exc_info 字段。
    formatter = JsonLogFormatter()
    payload = json.loads(formatter.format(_make_record()))
    assert "exc_info" not in payload


# --------------------------------------------------------------------------- #
# configure_json_logging（注意：用 fixture 还原全局 logging 状态）
# --------------------------------------------------------------------------- #


def test_configure_json_logging_installs_json_formatter(restore_root_logging):
    # 调用后根 logger 至少有一个 handler，且其 formatter 是 JsonLogFormatter。
    configure_json_logging()

    root = logging.getLogger()
    assert len(root.handlers) >= 1
    assert any(
        isinstance(h.formatter, JsonLogFormatter) for h in root.handlers
    )


def test_configure_json_logging_sets_level(restore_root_logging):
    # 应按参数设置根 logger 的 level。
    configure_json_logging(level=logging.DEBUG)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_json_logging_is_idempotent(restore_root_logging):
    # 幂等：重复调用先清空，不会累积多个 handler，数量保持稳定。
    configure_json_logging()
    count_after_first = len(logging.getLogger().handlers)

    configure_json_logging()
    configure_json_logging()
    count_after_repeat = len(logging.getLogger().handlers)

    assert count_after_first == count_after_repeat


# --------------------------------------------------------------------------- #
# PipelineMetrics
# --------------------------------------------------------------------------- #


def test_record_cycle_once_sets_totals_and_last_snapshot():
    # 记录一次周期后：累计量为本次值、last_* 为本次值、last_cycle_at == clock()。
    clock = FakeClock(BASE_TIME)
    metrics = PipelineMetrics(clock=clock)

    metrics.record_cycle(
        duration_seconds=1.5,
        markets_count=10,
        groups_count=4,
        opportunities_count=2,
        active_signals_count=3,
        signals_opened=2,
        signals_closed=1,
        alerts_fired=2,
    )

    assert metrics.cycles_total == 1
    assert metrics.signals_opened_total == 2
    assert metrics.signals_closed_total == 1
    assert metrics.alerts_fired_total == 2

    assert metrics.last_cycle_duration_seconds == 1.5
    assert metrics.last_markets_count == 10
    assert metrics.last_groups_count == 4
    assert metrics.last_opportunities_count == 2
    assert metrics.active_signals_count == 3
    assert metrics.last_cycle_at == BASE_TIME


def test_record_cycle_accumulates_totals_and_reflects_latest():
    # 多次记录后：累计量正确累加，last_* 反映最近一次，last_cycle_at 为最近时刻。
    clock = FakeClock(BASE_TIME)
    metrics = PipelineMetrics(clock=clock)

    metrics.record_cycle(
        duration_seconds=1.0,
        markets_count=5,
        groups_count=2,
        opportunities_count=1,
        active_signals_count=1,
        signals_opened=1,
        signals_closed=0,
        alerts_fired=1,
    )
    clock.advance(30)
    metrics.record_cycle(
        duration_seconds=2.0,
        markets_count=8,
        groups_count=3,
        opportunities_count=0,
        active_signals_count=2,
        signals_opened=3,
        signals_closed=2,
        alerts_fired=4,
    )

    # 累计量累加。
    assert metrics.cycles_total == 2
    assert metrics.signals_opened_total == 4
    assert metrics.signals_closed_total == 2
    assert metrics.alerts_fired_total == 5

    # last_* 反映最近一次。
    assert metrics.last_cycle_duration_seconds == 2.0
    assert metrics.last_markets_count == 8
    assert metrics.last_groups_count == 3
    assert metrics.last_opportunities_count == 0
    assert metrics.active_signals_count == 2
    assert metrics.last_cycle_at == BASE_TIME + timedelta(seconds=30)


def test_totals_are_monotonic_even_with_zero_deltas():
    # 累计量单调不减：传入 0 增量时累计量不变，但周期数仍 +1。
    metrics = PipelineMetrics(clock=FakeClock())
    metrics.record_cycle(
        duration_seconds=0.0,
        markets_count=0,
        groups_count=0,
        opportunities_count=0,
        active_signals_count=0,
        signals_opened=0,
        signals_closed=0,
        alerts_fired=0,
    )
    assert metrics.cycles_total == 1
    assert metrics.signals_opened_total == 0
    assert metrics.alerts_fired_total == 0


def test_snapshot_is_json_serializable_and_strips_clock():
    # snapshot() 可被 json.dumps 序列化、不含 clock 键、last_cycle_at 为 isoformat。
    clock = FakeClock(BASE_TIME)
    metrics = PipelineMetrics(clock=clock)
    metrics.record_cycle(
        duration_seconds=1.0,
        markets_count=5,
        groups_count=2,
        opportunities_count=1,
        active_signals_count=1,
        signals_opened=1,
        signals_closed=0,
        alerts_fired=1,
    )

    snap = metrics.snapshot()

    assert "clock" not in snap
    assert snap["last_cycle_at"] == BASE_TIME.isoformat()
    assert snap["cycles_total"] == 1
    assert snap["active_signals_count"] == 1
    # 可 JSON 序列化（不抛异常），且 round-trip 一致。
    encoded = json.dumps(snap)
    assert json.loads(encoded) == snap


def test_initial_snapshot_has_none_last_cycle_at():
    # 初始（未 record）快照的 last_cycle_at 为 None，且仍可 JSON 序列化。
    metrics = PipelineMetrics(clock=FakeClock())
    snap = metrics.snapshot()

    assert snap["last_cycle_at"] is None
    assert snap["cycles_total"] == 0
    assert "clock" not in snap
    json.dumps(snap)  # 不应抛异常
