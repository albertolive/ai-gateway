"""Hosted ai-gateway endpoint (Vercel function, stdlib only).

Serves an OpenAI-compatible POST /api/chat/completions. The `model` field is a
CASCADE name (general, code_review, creative, frontier, deepseek_cheap, ...), not
a provider model id — the whole point of the gateway is that repos name an
intent and models.json decides the provider/model/key.

Reuses scripts/gateway.py unchanged: it already does cascade resolution,
multi-key failover, daily-cap detection, and the wall-clock budget. This file is
only the HTTP adapter.

Env (all held centrally in the Vercel project, never in repos):
  GATEWAY_TOKEN        (required) inbound auth; repos send `Bearer $GATEWAY_TOKEN`
  OPENROUTER_API_KEY   OpenRouter provider
  GEMINI_API_KEY       Google AI Studio provider
  GROQ_API_KEY         Groq provider
  DEEPSEEK_API_KEY     DeepSeek provider
  AI_GATEWAY_API_KEY   Vercel AI Gateway provider (comma-separated = multi-account)
  ANTHROPIC_API_KEY    Anthropic (Claude) direct provider, frontier fallback tier
  OPENAI_API_KEY       OpenAI direct provider, frontier fallback tier
  AI_GATEWAY_BUDGET_S  cascade budget override (capped at 280s: Vercel Hobby's
                       300s invocation ceiling minus margin)

Deploy: `vercel.json` bundles this repo; Vercel serves api/chat/completions.py
at /api/chat/completions. Run locally with `scripts/serve.py`.

Every request emits one flat JSON `usage` line to stdout (cascade/model/status/
elapsed_ms) so fleet-wide usage is measurable; aggregate with `scripts/usage.py`.
"""

import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler

# gateway.py lives in scripts/ and reads models.json from the repo root; both
# are bundled with this function at deploy time (Vercel preserves the project
# layout, and the working directory is the project base). This file sits TWO
# levels deep (api/chat/), so scripts/ is ../../scripts from here.
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "..", "..", "scripts"))

import gateway  # noqa: E402

# Vercel Hobby caps a single invocation at 300s. The CI cascade budget is a
# measured 900s (fine for a low-volume review job); a request-path gateway must
# fail fast instead, so cap the budget here. Overridable downward only.
_SERVER_BUDGET_CAP = 280.0


def _budget():
    raw = os.environ.get("AI_GATEWAY_BUDGET_S", "").strip()
    try:
        value = float(raw) if raw else _SERVER_BUDGET_CAP
    except ValueError:
        value = _SERVER_BUDGET_CAP
    return min(value, _SERVER_BUDGET_CAP)


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def _log_usage(cascade, model, status, elapsed_ms):
    """Emit one parseable usage event per request for fleet-wide observability.

    Written to stderr (not stdout) so it is flushed immediately: Python buffers
    stdout when it is not a TTY, and a warm Vercel function does not flush that
    buffer between requests, so a final stdout line can be lost. stderr is
    line-buffered/unbuffered, so the event always lands. `scripts/usage.py`
    aggregates these from `vercel logs`. Flat JSON (no nesting) so a single
    regex can extract it from noisy log output.
    """
    print(json.dumps({
        "event": "usage",
        "cascade": cascade,
        "model": model,
        "status": status,
        "elapsed_ms": elapsed_ms,
        "created": int(time.time()),
    }), file=sys.stderr)


def _authorized(headers):
    token = os.environ.get("GATEWAY_TOKEN", "").strip()
    if not token:
        return False  # no token configured -> deny by default
    return headers.get("Authorization", "") == f"Bearer {token}"


def _parse(body):
    """Return (cascade, system, prompt) or raise ValueError with a clear reason."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    cascade = str(body.get("model") or "general").strip()
    if cascade not in gateway.CASCADES:
        valid = ", ".join(sorted(gateway.CASCADES))
        raise ValueError(f"unknown cascade '{cascade}' — use one of: {valid}")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    system = next((m.get("content") for m in messages
                   if isinstance(m, dict) and m.get("role") == "system"), None)
    prompt = "\n".join(
        str(m.get("content") or "")
        for m in messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ).strip()
    if not prompt:
        raise ValueError("messages must contain a user message with content")
    return cascade, system, prompt


def _send(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):  # health check for deploy verification
        _send(self, 200, {"ok": True, "service": "ai-gateway"})

    def do_POST(self):
        if not _authorized(self.headers):
            _send(self, 401, {"error": {"message": "invalid or missing GATEWAY_TOKEN"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            cascade, system, prompt = _parse(body)
        except ValueError as e:
            _send(self, 400, {"error": {"message": str(e)}})
            return
        except Exception:
            _send(self, 400, {"error": {"message": "invalid JSON body"}})
            return

        started = time.monotonic()
        try:
            text, used = gateway.complete(
                prompt,
                system=system,
                intent=cascade,
                temperature=float(body.get("temperature", 0.1)),
                budget_s=_budget(),
            )
        except Exception as e:
            # The cascade raises RuntimeError with the per-provider errors when
            # everything is skipped or down; surface it as an upstream failure.
            _log_usage(cascade, None, "error", _elapsed_ms(started))
            _send(self, 502, {"error": {"message": f"all providers failed: {e}"}})
            return
        _log_usage(cascade, used, "ok", _elapsed_ms(started))

        _send(self, 200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": used,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def log_message(self, *args):
        pass  # the cascade prints its own progress; suppress access-log noise
