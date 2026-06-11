"""可观测性：结构化日志与流水线指标（Phase Two · 切片 D）。

生产级信号工具需要可诊断：知道每个检测周期跑了多久、开了多少信号、当前活跃多少。
本模块提供两件轻量、零外部依赖的能力：

1. :func:`configure_json_logging` —— 把根日志切换为单行 JSON 输出，便于被集中式日志
   系统（如 ELK / Loki）采集与检索。可选启用，不影响默认的人类可读日志。
2. :class:`PipelineMetrics` —— 累计/快照式的流水线指标（周期数、信号开启/关闭累计、
   最近一次各阶段耗时、当前活跃信号数等），由流水线在每个周期更新，经 ``/metrics``
   端点暴露。

指标用注入的 ``clock`` 计时，使测试确定（不依赖真实墙钟时长）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JsonLogFormatter(logging.Formatter):
    """把日志记录格式化为单行 JSON 的 ``logging.Formatter``。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging(level: int = logging.INFO) -> None:
    """将根日志切换为结构化 JSON 输出（可选启用）。

    替换根 logger 的 handler 为一个使用 :class:`JsonLogFormatter` 的
    ``StreamHandler``。重复调用是幂等的（先清空已有 handler）。
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)


@dataclass
class PipelineMetrics:
    """流水线运行指标的累计 + 最近快照（Phase Two · 切片 D）。

    由 :meth:`record_cycle` 在每个检测周期结束时更新。累计量（``*_total``）单调不减；
    ``last_*`` 反映最近一次周期的快照。
    """

    clock: Clock = _utc_now

    # 累计量。
    cycles_total: int = 0
    signals_opened_total: int = 0
    signals_closed_total: int = 0
    alerts_fired_total: int = 0

    # 最近一次周期的快照。
    last_cycle_at: Optional[datetime] = None
    last_cycle_duration_seconds: float = 0.0
    last_markets_count: int = 0
    last_groups_count: int = 0
    last_opportunities_count: int = 0
    active_signals_count: int = 0

    def record_cycle(
        self,
        *,
        duration_seconds: float,
        markets_count: int,
        groups_count: int,
        opportunities_count: int,
        active_signals_count: int,
        signals_opened: int,
        signals_closed: int,
        alerts_fired: int,
    ) -> None:
        """记录一个检测周期的结果，更新累计量与最近快照。"""
        self.cycles_total += 1
        self.signals_opened_total += signals_opened
        self.signals_closed_total += signals_closed
        self.alerts_fired_total += alerts_fired

        self.last_cycle_at = self.clock()
        self.last_cycle_duration_seconds = duration_seconds
        self.last_markets_count = markets_count
        self.last_groups_count = groups_count
        self.last_opportunities_count = opportunities_count
        self.active_signals_count = active_signals_count

    def snapshot(self) -> Dict[str, object]:
        """返回可 JSON 序列化的指标快照（供 /metrics 端点）。"""
        data = asdict(self)
        data.pop("clock", None)
        if self.last_cycle_at is not None:
            data["last_cycle_at"] = self.last_cycle_at.isoformat()
        return data


__all__ = [
    "JsonLogFormatter",
    "configure_json_logging",
    "PipelineMetrics",
]
