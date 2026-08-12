"""Tests for the write tools this fork adds.

Two layers:
  * unit tests against a fake NetBoxRestClient (endpoint mapping, PATCH
    semantics, the delete confirmation guard, error surfacing);
  * an end-to-end test that runs the real tools against a stub NetBox HTTP
    server, so the actual HTTP method and payload are asserted - "the tool
    returned a dict" does not prove it sent a PATCH to the right URL.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import httpx
import pytest

from netbox_mcp_server import server as upstream
from netbox_mcp_server.netbox_client import NetBoxRestClient
from netbox_mcp_server.writes import write_tools


class FakeClient:
    """Records calls; mimics NetBoxRestClient's create/update/delete/get."""

    def __init__(self, obj=None):
        self.calls = []
        self.obj = obj if obj is not None else {"id": 1, "display": "thing"}

    def create(self, endpoint, data):
        self.calls.append(("create", endpoint, data))
        return {"id": 42, **data}

    def update(self, endpoint, id, data):
        self.calls.append(("update", endpoint, id, data))
        return {**self.obj, **data}

    def delete(self, endpoint, id):
        self.calls.append(("delete", endpoint, id))
        return True

    def get(self, endpoint, id=None, params=None, fallback_endpoint=None):
        self.calls.append(("get", endpoint, id))
        return self.obj


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(upstream, "netbox", client)
    return client


def test_create_maps_object_type_to_endpoint(fake_client):
    result = write_tools.netbox_create_object("ipam.ipaddress", {"address": "10.0.0.1/32"})

    assert fake_client.calls == [("create", "ipam/ip-addresses", {"address": "10.0.0.1/32"})]
    assert result["id"] == 42


def test_update_passes_only_given_fields(fake_client):
    write_tools.netbox_update_object("ipam.ipaddress", 1475, {"description": "new"})

    assert fake_client.calls == [("update", "ipam/ip-addresses", 1475, {"description": "new"})]


def test_unknown_object_type_is_rejected_before_any_call(fake_client):
    with pytest.raises(ValueError, match="Invalid object_type"):
        write_tools.netbox_update_object("ipam.nonsense", 1, {"description": "x"})

    assert fake_client.calls == []


def test_delete_requires_matching_display(fake_client):
    fake_client.obj = {"id": 1475, "display": "177.28.2.16/32"}

    with pytest.raises(ValueError, match="confirm_display mismatch"):
        write_tools.netbox_delete_object("ipam.ipaddress", 1475, "177.28.2.17/32")

    assert [c[0] for c in fake_client.calls] == ["get"]  # read only, no delete


def test_delete_with_matching_display_returns_predelete_state(fake_client):
    fake_client.obj = {"id": 1475, "display": "177.28.2.16/32", "description": "before"}

    result = write_tools.netbox_delete_object("ipam.ipaddress", 1475, "177.28.2.16/32")

    assert ("delete", "ipam/ip-addresses", 1475) in fake_client.calls
    assert result["deleted"] is True
    assert result["object"]["description"] == "before"


def test_delete_of_missing_object_reports_not_found(fake_client):
    fake_client.obj = {}

    with pytest.raises(ValueError, match="not found"):
        write_tools.netbox_delete_object("ipam.ipaddress", 999999, "whatever")


def test_netbox_validation_error_is_surfaced(monkeypatch):
    class Failing(FakeClient):
        def update(self, endpoint, id, data):
            request = httpx.Request("PATCH", "http://netbox/api/ipam/ip-addresses/1/")
            response = httpx.Response(
                400,
                json={"description": ["Ensure this field has no more than 200 characters."]},
                request=request,
            )
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(upstream, "netbox", Failing())

    with pytest.raises(ValueError, match="HTTP 400") as excinfo:
        write_tools.netbox_update_object("ipam.ipaddress", 1, {"description": "x" * 300})

    assert "HTTP 400" in str(excinfo.value)
    assert "no more than 200 characters" in str(excinfo.value)


def test_permission_denied_explains_the_cause(monkeypatch):
    class Denied(FakeClient):
        def update(self, endpoint, id, data):
            request = httpx.Request("PATCH", "http://netbox/api/ipam/ip-addresses/1/")
            response = httpx.Response(403, json={"detail": "permission denied"}, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(upstream, "netbox", Denied())

    with pytest.raises(ValueError, match="lacks add/change/delete permission"):
        write_tools.netbox_update_object("ipam.ipaddress", 1, {"description": "x"})


def test_uninitialised_client_is_reported(monkeypatch):
    monkeypatch.setattr(upstream, "netbox", None)

    with pytest.raises(RuntimeError, match="not initialised"):
        write_tools.netbox_create_object("ipam.ipaddress", {"address": "10.0.0.1/32"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("false", False),
        ("", False),
    ],
)
def test_writes_enabled_flag(monkeypatch, value, expected):
    monkeypatch.setenv(write_tools.ENABLE_WRITES_ENV, value)
    assert write_tools.writes_enabled() is expected


def test_writes_disabled_when_env_absent(monkeypatch):
    monkeypatch.delenv(write_tools.ENABLE_WRITES_ENV, raising=False)
    assert write_tools.writes_enabled() is False
    assert write_tools.register() == []


def test_register_adds_tools_to_the_upstream_mcp_instance(monkeypatch):
    monkeypatch.setenv(write_tools.ENABLE_WRITES_ENV, "true")

    registered = write_tools.register()

    assert registered == ["netbox_create_object", "netbox_update_object", "netbox_delete_object"]
    import asyncio

    names = {tool.name for tool in asyncio.run(upstream.mcp.list_tools())}
    for name in registered:
        assert name in names, f"{name} was not registered on the FastMCP instance"
    # the read tools must still be there
    assert "netbox_get_objects" in names

    # the description an LLM sees must carry the safety contract, not just a title
    delete_tool = asyncio.run(upstream.mcp.get_tool("netbox_delete_object"))
    assert "confirm_display" in (delete_tool.description or "")


# --------------------------------------------------------------------------
# End-to-end against a stub NetBox: proves the HTTP method, URL and body.
# --------------------------------------------------------------------------


class _StubNetBox(BaseHTTPRequestHandler):
    requests: ClassVar[list] = []
    obj: ClassVar[dict] = {"id": 1475, "display": "177.28.2.16/32", "description": "before"}

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _respond(self, status, payload=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        type(self).requests.append(("GET", self.path, None, self.headers.get("Authorization")))
        self._respond(200, type(self).obj)

    def do_POST(self):
        data = self._read_body()
        type(self).requests.append(("POST", self.path, data, self.headers.get("Authorization")))
        self._respond(201, {"id": 9001, **data})

    def do_PATCH(self):
        data = self._read_body()
        type(self).requests.append(("PATCH", self.path, data, self.headers.get("Authorization")))
        self._respond(200, {**type(self).obj, **data})

    def do_DELETE(self):
        type(self).requests.append(("DELETE", self.path, None, self.headers.get("Authorization")))
        self._respond(204)

    def log_message(self, *args):  # silence the stdlib access log
        pass


@pytest.fixture
def stub_netbox(monkeypatch):
    _StubNetBox.requests = []
    server = HTTPServer(("127.0.0.1", 0), _StubNetBox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = NetBoxRestClient(url=f"http://{host}:{port}", token="test-token", verify_ssl=False)
    monkeypatch.setattr(upstream, "netbox", client)
    try:
        yield _StubNetBox
    finally:
        server.shutdown()
        server.server_close()


def test_e2e_update_issues_patch_to_the_right_url(stub_netbox):
    result = write_tools.netbox_update_object("ipam.ipaddress", 1475, {"description": "after"})

    method, path, body, auth = stub_netbox.requests[-1]
    assert method == "PATCH"
    assert path == "/api/ipam/ip-addresses/1475/"
    assert body == {"description": "after"}
    assert auth == "Token test-token"
    assert result["description"] == "after"


def test_e2e_create_issues_post_to_the_collection_url(stub_netbox):
    write_tools.netbox_create_object("ipam.prefix", {"prefix": "10.9.0.0/24"})

    method, path, body, _ = stub_netbox.requests[-1]
    assert method == "POST"
    assert path == "/api/ipam/prefixes/"
    assert body == {"prefix": "10.9.0.0/24"}


def test_e2e_delete_reads_then_deletes(stub_netbox):
    write_tools.netbox_delete_object("ipam.ipaddress", 1475, "177.28.2.16/32")

    methods = [r[0] for r in stub_netbox.requests]
    assert methods == ["GET", "DELETE"]
    assert stub_netbox.requests[-1][1] == "/api/ipam/ip-addresses/1475/"


def test_e2e_delete_guard_sends_no_delete(stub_netbox):
    with pytest.raises(ValueError, match="confirm_display mismatch"):
        write_tools.netbox_delete_object("ipam.ipaddress", 1475, "wrong")

    assert [r[0] for r in stub_netbox.requests] == ["GET"]


def test_module_entrypoint_registers_before_running(monkeypatch):
    """`python -m netbox_mcp_server.writes` must register tools before handing over."""
    os.environ[write_tools.ENABLE_WRITES_ENV] = "true"
    order = []
    monkeypatch.setattr(write_tools, "register", lambda: order.append("register"))

    import netbox_mcp_server.writes.__main__ as entry

    monkeypatch.setattr(entry, "write_tools", write_tools)
    monkeypatch.setattr(entry, "main", lambda: order.append("main"))

    entry.run()

    assert order == ["register", "main"]


# --------------------------------------------------------------------------
# Canary for upstream merges: the write tools bind to these upstream names.
# If an upstream release renames one, this fails in the sync PR instead of at
# runtime in the cluster.
# --------------------------------------------------------------------------


def test_upstream_api_surface_we_depend_on():
    assert hasattr(upstream, "mcp"), "FastMCP instance"
    assert hasattr(upstream, "netbox"), "module-global NetBox client set by main()"
    assert callable(upstream._get_endpoint_info), "object_type -> endpoint resolver"
    assert "ipam.ipaddress" in upstream.NETBOX_OBJECT_TYPES

    endpoint, _fallback = upstream._get_endpoint_info("ipam.ipaddress")
    assert endpoint == "ipam/ip-addresses"

    from netbox_mcp_server.netbox_client import NetBoxRestClient

    for method in ("create", "update", "delete", "get"):
        assert callable(getattr(NetBoxRestClient, method)), method
