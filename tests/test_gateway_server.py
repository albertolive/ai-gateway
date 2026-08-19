"""Tests for the hosted gateway handler (api/chat/completions.py).

Loads the handler module the way Vercel does and drives it over a real
in-process HTTP server, with gateway.complete() stubbed so no provider keys or
network access are needed.
"""

import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import gateway

_HANDLER_PATH = os.path.join(os.path.dirname(__file__), "..", "api",
                             "chat", "completions.py")


def _load_handler():
    spec = importlib.util.spec_from_file_location("gateway_handler", _HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gw(monkeypatch):
    mod = _load_handler()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GATEWAY_TOKEN", "secret-token")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _post(base, body, token="secret-token"):
    req = urllib.request.Request(
        f"{base}/api/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_health_check(gw):
    with urllib.request.urlopen(f"{gw}/api/chat/completions", timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert resp.status == 200
    assert body["ok"] is True


def test_completion_returns_openai_shape(gw, monkeypatch):
    monkeypatch.setattr(gateway, "complete",
                        lambda *a, **kw: ("hello from cascade", "openrouter/foo"))
    status, body = _post(gw, {"model": "general",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "hello from cascade"
    assert body["model"] == "openrouter/foo"
    assert body["object"] == "chat.completion"


def test_model_field_is_treated_as_cascade(gw, monkeypatch):
    seen = {}

    def fake_complete(prompt, system=None, intent="general", **kw):
        seen["intent"] = intent
        seen["system"] = system
        seen["prompt"] = prompt
        seen["budget"] = kw.get("budget_s")
        return "ok", "gemini/x"

    monkeypatch.setattr(gateway, "complete", fake_complete)
    _post(gw, {"model": "creative",
               "messages": [{"role": "system", "content": "sys"},
                            {"role": "user", "content": "user prompt"}]})
    assert seen["intent"] == "creative"
    assert seen["system"] == "sys"
    assert "user prompt" in seen["prompt"]
    assert seen["budget"] is not None and seen["budget"] <= 280.0


def test_unknown_cascade_is_400(gw):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(gw, {"model": "nope",
                   "messages": [{"role": "user", "content": "hi"}]})
    assert exc.value.code == 400


def test_wrong_token_is_401(gw):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(gw, {"messages": [{"role": "user", "content": "hi"}]},
              token="wrong")
    assert exc.value.code == 401


def test_missing_user_message_is_400(gw):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(gw, {"model": "general",
                   "messages": [{"role": "system", "content": "no user"}]})
    assert exc.value.code == 400


def test_all_providers_fail_is_502(gw, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("everything down")

    monkeypatch.setattr(gateway, "complete", boom)
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(gw, {"messages": [{"role": "user", "content": "hi"}]})
    assert exc.value.code == 502
