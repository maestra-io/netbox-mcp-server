"""Shared fixtures: a stub NetBox HTTP server for end-to-end write tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from netbox_mcp_server import server as upstream
from netbox_mcp_server.netbox_client import NetBoxRestClient


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
        data = self._read_body() or None
        type(self).requests.append(("DELETE", self.path, data, self.headers.get("Authorization")))
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
