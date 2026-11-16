"""The optional agent ``reason`` on every tool, and one log line per tool call.

issues-maestra#1768. ``reason`` is never required and never rejects a call:
absent, empty, the wrong type, overlong or sent to any tool, the call runs.
The middleware declares it in every tool's input schema, removes it before
FastMCP validates the arguments (FastMCP rejects an undeclared argument),
keeps it for the call in ``current_reason`` (write_tools puts it next to the
person in the NetBox changelog message) and writes one JSON line per call to
stderr (stdout is the stdio transport).
"""

import json
import sys
import time
from contextvars import ContextVar
from typing import Any

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware

from netbox_mcp_server.writes import access

REASON_DESCRIPTION = (
    "Optional free-form context from the agent: the user's underlying task, what this call "
    "is trying to answer, or why specific params were chosen. Logged for review; never "
    "affects results. No secrets or customer personal data."
)
REASON_SCHEMA = {"type": "string", "description": REASON_DESCRIPTION}
REASON_LOG_CHARS = 300

current_reason: ContextVar[str] = ContextVar("current_reason", default="")


def reason_text(value: Any) -> str:
    """Whatever the caller sent, as clipped text; "" for absent or blank."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, default=str, ensure_ascii=False)
    return value.strip()[:REASON_LOG_CHARS]


class AgentReasonMiddleware(Middleware):
    """Declares, strips and logs ``reason``; never raises because of it."""

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        tools = await call_next(context)
        return [
            tool.model_copy(
                update={
                    "parameters": {
                        **tool.parameters,
                        "properties": {
                            **tool.parameters.get("properties", {}),
                            "reason": REASON_SCHEMA,
                        },
                    }
                }
            )
            for tool in tools
        ]

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        arguments = context.message.arguments
        # ponytail: no tool here has a `reason` of its own; one that ever does needs it
        # skipped here.
        raw = arguments.pop("reason", None) if isinstance(arguments, dict) else None
        reason = reason_text(raw)
        email = access.verified_email() if access.enabled() else None
        headers = get_http_headers()
        token = current_reason.set(reason)
        started = time.monotonic()
        outcome = "error"
        try:
            result = await call_next(context)
            outcome = "ok"
            return result
        finally:
            current_reason.reset(token)
            line = {
                "msg": "tool call",
                "server": "netbox",
                "tool": context.message.name,
                "outcome": outcome,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "user": email,
                "user_source": "cf_access" if email else "none",
                "reason": reason or None,
                "reason_present": bool(reason),
                "cf_ray": headers.get("cf-ray"),
            }
            sys.stderr.write(json.dumps(line, ensure_ascii=False) + "\n")
            sys.stderr.flush()
