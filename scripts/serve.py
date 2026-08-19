#!/usr/bin/env python3
"""Run the hosted gateway locally (no Vercel deploy needed for development).

Loads the same handler Vercel serves (api/chat/completions.py) over a plain
stdlib HTTP server, so you can curl it exactly as a repo would in production.

Usage:
    GATEWAY_TOKEN=... OPENROUTER_API_KEY=... ./scripts/serve.py
    curl -s localhost:8787/api/chat/completions \
        -H "Authorization: Bearer $GATEWAY_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"model":"general","messages":[{"role":"user","content":"Say OK."}]}'
"""

import importlib.util
import os
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_HANDLER = os.path.join(_HERE, "..", "api", "chat", "completions.py")


def _load_handler():
    spec = importlib.util.spec_from_file_location("gateway_handler", _HANDLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    handler = _load_handler().handler
    print(f"ai-gateway dev server -> http://localhost:{port}/api/chat/completions")
    print("required env: GATEWAY_TOKEN (+ any provider keys)")
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()
