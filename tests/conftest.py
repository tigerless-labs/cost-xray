"""Shared fixtures: a minimal fake mitmproxy `flow` so addon.py can be tested
without running a real proxy.

The addon only ever touches a small, well-defined slice of the mitmproxy flow API
(headers, path, content, timestamp, status, body text). We model exactly that slice
so the tests stay fast and dependency-free — addon.py imports no mitmproxy at all.
"""
from __future__ import annotations

import json

import pytest


class FakeConn:
    def __init__(self, conn_id: str = "abcdef0123456789"):
        self.id = conn_id


class FakeRequest:
    def __init__(self, *, path="/v1/messages", headers=None, content=b"",
                 host="api.anthropic.com", timestamp_start=1_700_000_000.0):
        self.path = path
        self.headers = dict(headers or {})
        self.content = content if isinstance(content, (bytes, str)) else json.dumps(content)
        self.host = host
        self.timestamp_start = timestamp_start


class FakeResponse:
    def __init__(self, *, text="", headers=None, status_code=200):
        self._text = text
        self.headers = dict(headers or {})
        self.status_code = status_code

    def get_text(self, strict=False):
        return self._text


class FakeFlow:
    def __init__(self, request: FakeRequest, response: FakeResponse | None = None,
                 conn_id="abcdef0123456789"):
        self.request = request
        self.response = response
        self.client_conn = FakeConn(conn_id)


@pytest.fixture
def make_flow():
    """Factory: build a FakeFlow from a request body dict (+ optional response)."""
    def _make(body=None, *, path="/v1/messages", req_headers=None,
              resp_text="", resp_headers=None, status_code=200, conn_id="abcdef0123456789",
              raw_content=None):
        if raw_content is not None:
            content = raw_content
        elif body is not None:
            content = json.dumps(body)
        else:
            content = b""
        req = FakeRequest(path=path, headers=req_headers, content=content)
        resp = FakeResponse(text=resp_text, headers=resp_headers, status_code=status_code)
        return FakeFlow(req, resp, conn_id=conn_id)
    return _make
