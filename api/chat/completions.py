"""Hosted ai-gateway endpoint (Vercel function, stdlib only).

Serves an OpenAI-compatible POST /api/chat/completions. The `model` field is a
CASCADE name (general, code_review, creative, frontier, deepseek_cheap, ...), not
a provider model id — the whole point of the gateway is that repos name an
intent and models.json decides the provider/model/key.

Reuses scripts/gateway.py unchanged: it already does cascade resolution,
multi-key failover, daily-cap detection, and the wall-clock budget. This file is
only the HTTP adapter.

Request shape is OpenAI chat-completions: `messages` (content may be a string or
an array of parts — image_url parts pass through unchanged for vision), plus
optional `response_format` (json_schema with a strict schema, or json_object)
or a top-level `schema` object for structured outputs. `model` is still a
cascade name; the response carries the provider that actually served it.

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


def _flatten_text(content):
    """Text of a message content (string or array of text parts).

    Image parts are dropped here — this mirror is only for the prompt-building
    path and observability; the request forwarded to the cascade keeps parts.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text") for p in content
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
        ).strip()
    return ""


def _parse_schema(body):
    """Extract (schema, schema_name) from response_format or a top-level schema.

    Accepts the OpenAI shape (response_format.json_schema.schema) and a simple
    top-level `schema` object (+ optional schema_name) for json_object mode.
    Returns (None, "response") when the client asked for no structured output.
    """
    rf = body.get("response_format")
    if rf is not None:
        if not isinstance(rf, dict):
            raise ValueError("response_format must be an object")
        rf_type = rf.get("type")
        if rf_type == "json_schema":
            js = rf.get("json_schema") or {}
            schema = js.get("schema")
            if not isinstance(schema, dict):
                raise ValueError(
                    "response_format.json_schema.schema must be an object")
            return schema, str(js.get("name") or "response")
        if rf_type == "json_object":
            # Loose JSON mode: a top-level `schema` (if any) is embedded in the
            # prompt for providers without strict json_schema (groq/deepseek).
            schema = body.get("schema")
            return (schema if isinstance(schema, dict) else None), \
                str(body.get("schema_name") or "response")
        raise ValueError(f"unsupported response_format type {rf_type!r} — "
                         "use json_schema or json_object")
    schema = body.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise ValueError("schema must be an object")
    return schema, str(body.get("schema_name") or "response")


def _parse(body):
    """Return (cascade, system, prompt, messages, schema, schema_name).

    Raises ValueError with a clear reason. messages are validated and forwarded
    VERBATIM to the cascade so image content parts survive (vision); system and
    prompt are flattened text mirrors for the prompt path and observability.
    """
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    cascade = str(body.get("model") or "general").strip()
    if cascade not in gateway.CASCADES:
        valid = ", ".join(sorted(gateway.CASCADES))
        raise ValueError(f"unknown cascade '{cascade}' — use one of: {valid}")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") not in ("system", "user",
                                                             "assistant"):
            raise ValueError(f"messages[{i}] must be an object with role "
                             "system, user or assistant")
        content = m.get("content")
        if isinstance(content, str):
            if not content.strip():
                raise ValueError(f"messages[{i}] content cannot be empty")
        elif isinstance(content, list):
            if not content:
                raise ValueError(f"messages[{i}] content array cannot be empty")
            for part in content:
                if isinstance(part, str):
                    continue
                if not isinstance(part, dict) or "type" not in part:
                    raise ValueError(f"messages[{i}] has a malformed content part")
        else:
            raise ValueError(f"messages[{i}] content must be a string or an "
                             "array of parts")
    system = next((_flatten_text(m.get("content")) for m in messages
                   if m.get("role") == "system"), None)
    prompt = "\n".join(
        _flatten_text(m.get("content"))
        for m in messages
        if m.get("role") in ("user", "assistant")
    ).strip()
    if not prompt:
        raise ValueError("messages must contain a user message with content")
    schema, schema_name = _parse_schema(body)
    return cascade, system, prompt, messages, schema, schema_name


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
            cascade, system, prompt, messages, schema, schema_name = _parse(body)
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
                messages=messages,
                schema=schema,
                schema_name=schema_name,
            )
        except Exception as e:
            # The cascade raises RuntimeError with the per-provider errors when
            # everything is skipped or down; surface it as an upstream failure.
            _log_usage(cascade, None, "error", _elapsed_ms(started))
            _send(self, 502, {"error": {"message": f"all providers failed: {e}"}})
            return
        _log_usage(cascade, used, "ok", _elapsed_ms(started))

        # Structured mode returns the parsed object; OpenAI clients expect a JSON
        # STRING in choices[0].message.content, so serialize it back.
        content = json.dumps(text, ensure_ascii=False) if schema else text
        _send(self, 200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": used,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def log_message(self, *args):
        pass  # the cascade prints its own progress; suppress access-log noise
