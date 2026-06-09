"""Shared automation planning capability metadata."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


HOUZZKIT_AI_INITIALIZE_METADATA_KEY = "houzzkit_ai"
MCP_SERVER_VERSION = "2.3.0"
SUPPORTED_PLAN_FEATURES = ["time_trigger_date", "time_trigger_delay"]


def automation_initialize_metadata(local_timezone: Any) -> dict[str, Any] | None:
    """Build MCP initialize metadata consumed by ai-server automation Plan Mode."""
    if not isinstance(local_timezone, str) or not local_timezone.strip():
        return None
    try:
        ZoneInfo(local_timezone)
    except ZoneInfoNotFoundError:
        return None

    # ai-server 使用这个 IANA 时区归一化“明天”等相对日期，不能用系统时区兜底。
    return {
        "local_timezone": local_timezone,
        "supported_plan_features": list(SUPPORTED_PLAN_FEATURES),
    }
