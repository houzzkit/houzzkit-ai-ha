"""Shared automation planning capability metadata."""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


HOUZZKIT_AI_INITIALIZE_METADATA_KEY = "houzzkit_ai"
MCP_SERVER_VERSION = "2.3.0"
TASK_PLAN_FEATURE = "task_plan"


def task_plan_initialize_context(local_timezone: Any) -> dict[str, dict[str, Any]] | None:
    """Build Task Plan initialize capability and metadata for ai-server."""
    if not isinstance(local_timezone, str) or not local_timezone.strip():
        return None
    timezone_name = local_timezone.strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None

    # ai-server 使用这个 IANA 时区归一化相对日期，不能用系统时区兜底。
    return {
        "capability": {"features": {TASK_PLAN_FEATURE: True}},
        "meta": {"local_timezone": timezone_name},
    }


def inject_houzzkit_ai_initialize_meta(
    payload: dict[str, Any],
    houzzkit_ai_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Inject Houzzkit AI _meta into an MCP initialize JSON-RPC response payload."""
    if not houzzkit_ai_meta:
        return payload

    result = payload.get("result")
    if not isinstance(result, dict):
        return payload
    initialize_keys = ("protocolVersion", "capabilities", "serverInfo")
    if not all(key in result for key in initialize_keys):
        return payload

    meta = result.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        result["_meta"] = meta

    existing = meta.get(HOUZZKIT_AI_INITIALIZE_METADATA_KEY)
    houzzkit_ai = dict(existing) if isinstance(existing, dict) else {}
    houzzkit_ai.update(houzzkit_ai_meta)
    meta[HOUZZKIT_AI_INITIALIZE_METADATA_KEY] = houzzkit_ai
    return payload
