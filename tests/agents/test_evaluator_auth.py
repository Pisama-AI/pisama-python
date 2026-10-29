"""Wire-conformance tests for PisamaEvaluator auth.

``POST /api/v1/evaluate`` accepts only a JWT minted from the API key via
``POST /api/v1/auth/token``; a raw API key as bearer gets 401. These tests run
the evaluator against a real local HTTP server that enforces that contract.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pisama.agents.evaluator import PisamaEvaluator

API_KEY = "psk_wire_test"
EVALUATE_PATH = "/api/v1/evaluate"
TOKEN_PATH = "/api/v1/auth/token"
RESULT = {"passed": True, "score": 0.9, "failures": [], "detectors_run": ["spec"]}


class _PlatformHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        auth = self.headers.get("Authorization")
        state["requests"].append({"path": self.path, "authorization": auth, "body": body})
        if self.path == TOKEN_PATH:
            state["tokens_minted"] += 1
            self._reply(200, {"access_token": f"jwt-{state['tokens_minted']}"})
        elif self.path == EVALUATE_PATH:
            if not (auth or "").startswith("Bearer jwt-"):
                self._reply(401, {"detail": "Invalid token"})
            elif state["evaluate_401s_remaining"] > 0:
                state["evaluate_401s_remaining"] -= 1
                self._reply(401, {"detail": "Token has expired"})
            else:
                self._reply(200, RESULT)
        else:
            self._reply(404, {"detail": "not found"})

    def _reply(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def platform_stand_in():
    server = HTTPServer(("127.0.0.1", 0), _PlatformHandler)
    server.state = {  # type: ignore[attr-defined]
        "requests": [],
        "tokens_minted": 0,
        "evaluate_401s_remaining": 0,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.state  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _assert_exchanged_then_evaluated(state, expected_paths) -> None:
    assert [r["path"] for r in state["requests"]] == expected_paths
    assert state["requests"][0]["body"]["api_key"] == API_KEY
    for r in state["requests"]:
        assert r["authorization"] != f"Bearer {API_KEY}"


def test_evaluate_exchanges_key_for_jwt(platform_stand_in):
    url, state = platform_stand_in
    with PisamaEvaluator(API_KEY, base_url=url) as evaluator:
        result = evaluator.evaluate({"goal": "x"}, {"text": "y"})
    assert result.passed and result.detectors_run == ["spec"]
    _assert_exchanged_then_evaluated(state, [TOKEN_PATH, EVALUATE_PATH])
    assert state["requests"][1]["authorization"] == "Bearer jwt-1"


def test_evaluate_reexchanges_once_on_expired_jwt(platform_stand_in):
    url, state = platform_stand_in
    state["evaluate_401s_remaining"] = 1
    with PisamaEvaluator(API_KEY, base_url=url) as evaluator:
        assert evaluator.evaluate({"goal": "x"}, {"text": "y"}).passed
    _assert_exchanged_then_evaluated(
        state, [TOKEN_PATH, EVALUATE_PATH, TOKEN_PATH, EVALUATE_PATH]
    )
    assert state["requests"][3]["authorization"] == "Bearer jwt-2"


async def test_evaluate_async_exchanges_key_for_jwt(platform_stand_in):
    url, state = platform_stand_in
    with PisamaEvaluator(API_KEY, base_url=url) as evaluator:
        result = await evaluator.evaluate_async({"goal": "x"}, {"text": "y"})
    assert result.passed
    _assert_exchanged_then_evaluated(state, [TOKEN_PATH, EVALUATE_PATH])
