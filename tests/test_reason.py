"""The optional agent ``reason`` (writes/reason.py): never rejects a call, logged once per call."""

import asyncio
import json

import pytest
from fastmcp import Client, FastMCP

from netbox_mcp_server import server as upstream
from netbox_mcp_server.writes import access, reason, write_tools

EMAIL = "ivonin@maestra.io"


def _probe() -> FastMCP:
    mcp = FastMCP("t", middleware=[reason.AgentReasonMiddleware()])

    @mcp.tool
    def echo(q: str) -> str:
        """Returns q and the reason the tool sees in its own context (a sync tool)."""
        return f"{q}|{reason.current_reason.get()}"

    mcp.tool(write_tools.netbox_update_object)
    return mcp


def _call(mcp: FastMCP, tool: str, arguments: dict):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(run())


def _lines(capsys) -> list[dict]:
    err = capsys.readouterr().err.splitlines()
    return [json.loads(x) for x in err if x.startswith('{"msg": "tool call"')]


def test_every_tool_lists_an_optional_reason(monkeypatch):
    monkeypatch.setattr(
        upstream.mcp, "middleware", [*upstream.mcp.middleware, reason.AgentReasonMiddleware()]
    )
    monkeypatch.setenv(write_tools.ENABLE_WRITES_ENV, "true")
    write_tools.register()

    async def run():
        async with Client(upstream.mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(run())
    assert {"netbox_get_objects", "netbox_delete_object"} <= {t.name for t in tools}
    for tool in tools:
        assert tool.inputSchema["properties"]["reason"] == reason.REASON_SCHEMA
        assert "reason" not in tool.inputSchema.get("required", [])


@pytest.mark.parametrize(
    ("sent", "logged"),
    [
        ("user wants the rack of host x", "user wants the rack of host x"),
        ("", None),
        ("   ", None),
        (None, None),
        ("x" * 10_000, "x" * reason.REASON_LOG_CHARS),
        (42, "42"),
        ({"why": "nested"}, '{"why": "nested"}'),
    ],
)
def test_any_reason_runs_the_call_and_is_logged(capsys, sent, logged):
    result = _call(_probe(), "echo", {"q": "hi", "reason": sent})

    assert not result.is_error
    assert result.data == f"hi|{logged or ''}"  # stripped from the arguments, kept in context
    [line] = _lines(capsys)
    assert (line["server"], line["tool"], line["outcome"]) == ("netbox", "echo", "ok")
    assert line["reason"] == logged
    assert line["reason_present"] is (logged is not None)
    assert (line["user"], line["user_source"]) == (None, "none")
    assert reason.current_reason.get() == ""


def test_a_call_without_reason_runs_and_is_logged(capsys):
    result = _call(_probe(), "echo", {"q": "hi"})

    assert not result.is_error
    [line] = _lines(capsys)
    assert (line["reason"], line["reason_present"]) == (None, False)


def test_a_failing_call_is_logged_as_error(capsys):
    result = _call(_probe(), "echo", {"reason": "why"})  # q missing: the tool's own validation

    assert result.is_error
    [line] = _lines(capsys)
    assert (line["outcome"], line["reason"]) == ("error", "why")


class _FakeClient:
    def __init__(self):
        self.calls = []

    def update(self, endpoint, id, data):
        self.calls.append(data)
        return {"id": id, **data}


@pytest.fixture
def access_mode(monkeypatch):
    monkeypatch.setattr(access, "_enabled", True)
    monkeypatch.setattr(access, "verified_email", lambda: EMAIL)
    client = _FakeClient()
    monkeypatch.setattr(upstream, "netbox", client)
    return client


def test_the_reason_goes_next_to_the_person_in_the_changelog(capsys, access_mode):
    arguments = {"object_type": "ipam.prefix", "object_id": 7, "data": {"description": "x"}}
    result = _call(_probe(), "netbox_update_object", {**arguments, "reason": "fix\n  the desc"})

    assert not result.is_error
    assert access_mode.calls[0]["changelog_message"] == (
        f"via netbox-mcp by {EMAIL} (reason: fix the desc)"
    )
    [line] = _lines(capsys)
    assert (line["user"], line["user_source"], line["reason"]) == (
        EMAIL,
        "cf_access",
        "fix\n  the desc",
    )

    data = {"description": "x", "changelog_message": "ticket 4137"}
    _call(_probe(), "netbox_update_object", {**arguments, "data": data, "reason": "y" * 500})
    message = access_mode.calls[1]["changelog_message"]
    assert message.startswith(f"via netbox-mcp by {EMAIL} (reason: yyy")
    assert len(message) == write_tools.CHANGELOG_MESSAGE_MAX


def test_no_reason_leaves_the_changelog_as_before(access_mode):
    arguments = {"object_type": "ipam.prefix", "object_id": 7, "data": {"description": "x"}}
    _call(_probe(), "netbox_update_object", {**arguments, "reason": " "})

    assert access_mode.calls[0]["changelog_message"] == f"via netbox-mcp by {EMAIL}"


def test_over_http_the_line_carries_the_verified_person_and_the_ray(capsys, monkeypatch):
    from starlette.testclient import TestClient

    from tests.test_access import _HEADERS, KEY, Jwks, _jwk, _token, _verifier

    monkeypatch.setattr(access, "_enabled", True)
    mcp = _probe()
    mcp.auth = _verifier(Jwks([_jwk(KEY, "k1")]))
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"q": "hi", "reason": "why"}},
    }
    headers = {**_HEADERS, "Cf-Access-Jwt-Assertion": _token(), "Cf-Ray": "8f00-AMS"}
    with TestClient(mcp.http_app(stateless_http=True, json_response=True)) as client:
        response = client.post("/mcp/", json=call, headers=headers)

    assert response.json()["result"]["content"][0]["text"] == "hi|why"
    [line] = _lines(capsys)
    assert (line["user"], line["user_source"], line["cf_ray"]) == (EMAIL, "cf_access", "8f00-AMS")
