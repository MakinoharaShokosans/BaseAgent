"""事件总线、轨迹、日志、追踪、脱敏。横切旁路，不依赖业务层。

设计文档：docs/08_observability/
"""

from agent_runtime.observability.event_store import EventStore, EventStoreError
from agent_runtime.observability.events import (
    HEARTBEAT,
    SCHEMA_VERSION,
    STREAM_DROPPED,
    TERMINAL_EVENTS,
    Event,
    clip,
    sanitize_payload,
)
from agent_runtime.observability.hub import STREAM_GAP, EventHub
from agent_runtime.observability.logs import setup_logging
from agent_runtime.observability.redaction import REDACTED, Redactor
from agent_runtime.observability.trajectory import TrajectoryRecorder, to_record

__all__ = [
    "HEARTBEAT",
    "REDACTED",
    "SCHEMA_VERSION",
    "STREAM_DROPPED",
    "STREAM_GAP",
    "TERMINAL_EVENTS",
    "Event",
    "EventHub",
    "EventStore",
    "EventStoreError",
    "Redactor",
    "TrajectoryRecorder",
    "clip",
    "sanitize_payload",
    "setup_logging",
    "to_record",
]
