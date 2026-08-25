"""Multi-provider free-LLM gateway with cascading failover.

Providers are tried in order; any provider whose API key env var is missing
is skipped. All providers use OpenAI-compatible /chat/completions endpoints,
so only Python stdlib is needed (no pip install in CI).

Verified July 2026:
- OpenRouter free tier: ~20 req/min, ~200 req/day shared across :free models.
- Google AI Studio: gemini-2.0-flash free tier (OpenAI-compat endpoint,
  model name WITHOUT the "google/" prefix). 15 RPM, 1,500 RPD.
- Groq free tier: llama-3.3-70b-versatile 30 RPM / 1K RPD.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Providers and cascades live in models.json (repo root) so the model-watch
# bot can update them via PR without touching code, and so app repos calling
# the gateway config directly (see app-callers/) share the same source of
# truth. structured: "json_schema" -> strict structured outputs;
# "json_object" -> schema embedded in prompt.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "models.json")


def _load_providers(path=_CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["providers"]


PROVIDERS = _load_providers()


def load_cascades(path=_CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        cascades = json.load(f)["cascades"]
    resolved = {}
    for intent, entries in cascades.items():
        resolved[intent] = []
        for e in entries:
            p = PROVIDERS[e["provider"]]
            resolved[intent].append({
                "name": f"{e['provider']}/{e['model']}",
                "url": p["url"], "key_env": p["key_env"],
                "model": e["model"], "structured": e.get("structured", "json_object"),
                # vision: False on entries whose model rejects image content parts
                # (deepseek, groq's llama) so a vision request SKIPS them instead of
                # burning a guaranteed 400 round-trip per provider before failing over.
                "vision": e.get("vision", True),
            })
    return resolved


CASCADES = load_cascades()


# A 429 whose body names a DAILY allowance or a spent credit/balance is not worth retrying: the
# quota resets tomorrow, or only after a top-up, not in five seconds. Both caps observed on
# 2026-07-28 were of this kind ("Quota exceeded for metric ... PerDay" from Gemini, "Rate limit
# exceeded: free-models-per-day" from OpenRouter), and the retry spent a full extra request
# round-trip per dead provider to learn nothing. This also covers a Vercel account that has run
# out of credits ("insufficient credits/balance") — the fix is the next account's key, not a sleep.
# A per-MINUTE limit is the opposite case and still gets its retry, because five seconds genuinely
# helps there.
_NO_RETRY_CAP = re.compile(
    r"per[-\s]?day|daily|\bRPD\b|free-models-per-day"
    r"|insufficient|credits?|balance|billing|payment",
    re.I,
)

# Wall clock per single provider attempt (seconds). Free-tier providers
# occasionally queue generations for 60-90s+ (Gemini flash-lite, Aug 2026);
# a stalled attempt should fail over to the next cascade entry rather than
# hold the client. Tunable via AI_GATEWAY_ATTEMPT_TIMEOUT_S.
def _attempt_timeout_s():
    raw = os.environ.get("AI_GATEWAY_ATTEMPT_TIMEOUT_S", "")
    try:
        val = float(raw) if raw.strip() else 30.0
    except ValueError:
        print(f"  ! ignoring invalid AI_GATEWAY_ATTEMPT_TIMEOUT_S={raw!r}, using 30s")
        val = 30.0
    return max(5.0, min(val, 240.0))


def _provider_keys(key_env):
    """Return all configured keys for a provider, in failover order.

    One env var can hold several keys separated by commas or whitespace, so a
    single secret can back multiple accounts — e.g. several Vercel accounts,
    each with its own monthly credit pool. The gateway tries each key in order
    and fails over to the next account's key when one hits its quota.
    """
    return [k for k in re.split(r"[,\s]+", os.environ.get(key_env, "")) if k]


def _post_chat(base_url, api_key, payload, timeout=120):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional, ignored by others)
            "HTTP-Referer": "https://github.com",
            "X-Title": "ai-gateway",
            # Cloudflare in front of some providers (groq: error 1010) bans
            # the default Python-urllib signature outright; a named client
            # passes while still being honest about what we are.
            "User-Agent": "ai-gateway/1.0 (fleet caption/categorization client)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def _has_image_parts(messages):
    """True when any message carries a non-text content part (image_url etc.).

    The hosted endpoint passes OpenAI-style message arrays through, so content
    may be a string or a list of {type: text|image_url|...} parts. A cascade
    entry flagged vision: false cannot answer such a request, so it is skipped
    instead of burning a guaranteed-failing round-trip.
    """
    if not messages:
        return False
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") != "text":
                    return True
    return False


def _messages_with_schema_instruction(messages, schema, json_object_mode):
    """Forward client messages verbatim, embedding `schema` into the system turn.

    json_object providers enforce nothing server-side (the schema must live in
    the prompt), mirroring what the prompt path does with sys_content. Returns
    shallow copies; the caller's list is never mutated.
    """
    out = [dict(m) for m in messages]
    if not (schema and json_object_mode):
        return out
    instr = ("Respond ONLY with a single valid JSON object matching this "
             "JSON Schema exactly:\n" + json.dumps(schema))
    for m in out:
        if m.get("role") == "system":
            cur = m.get("content")
            if isinstance(cur, list):
                m["content"] = [{"type": "text", "text": instr}] + list(cur)
            else:
                m["content"] = (cur or "") + "\n\n" + instr
            return out
    out.insert(0, {"role": "system", "content": instr})
    return out


def complete(prompt, system=None, intent="general", schema=None,
             schema_name="response", temperature=0.1, max_retries_per_provider=1,
             budget_s=None, messages=None):
    """Run prompt through the cascade. Returns (text, provider_name).

    If `schema` (a JSON Schema dict) is given, output is requested/validated
    as JSON and the parsed object is returned instead of raw text.

    `budget_s` caps the WALL CLOCK for the whole cascade (default 900, or
    AI_GATEWAY_BUDGET_S). Without it the worst case was unbounded: 7 providers x 2 attempts x a
    120s socket timeout is ~28 minutes on paper, and because urlopen's timeout applies per socket
    operation rather than to total elapsed time, one slow responder stretched a real run to 44
    minutes on 2026-07-29. On a private repo that is billable CI time spent discovering that every
    provider is down. The budget converts an open-ended stall into a bounded, reported failure.

    900 is measured, not picked. Every SUCCESSFUL AI PR Review in the week to 2026-07-29 took
    between 312s and 666s, so a tighter budget would have killed real work: a 300s default would
    have failed all six.    900 clears the observed maximum by ~35% while still cutting the
    pathological case from 44 minutes to 15. Lower it only against fresh timings.

    `messages` (optional) forwards OpenAI-style message objects verbatim instead of
    building them from prompt/system — needed for vision (image content parts) and
    for the hosted endpoint to pass client messages through unchanged. When a
    request contains image parts, cascade entries flagged `vision: false` in
    models.json are skipped rather than tried. `schema`/`schema_name` work as
    before; the caller gets the parsed object back on success.
    """
    cascade = CASCADES.get(intent, CASCADES["general"])
    errors = []
    if budget_s is None:
        # A malformed override must not raise out of here: complete()'s callers catch RuntimeError
        # (review.py turns it into an outage comment), so a ValueError would escape as an unhandled
        # crash and be reported as a code failure, which is the exact thing this file is fixing.
        raw = os.environ.get("AI_GATEWAY_BUDGET_S", "")
        try:
            budget_s = float(raw) if raw.strip() else 900.0
        except ValueError:
            print(f"  ! ignoring invalid AI_GATEWAY_BUDGET_S={raw!r}, using 900s")
            budget_s = 900.0
    started = time.monotonic()
    left = lambda: budget_s - (time.monotonic() - started)  # noqa: E731

    needs_vision = _has_image_parts(messages)

    for provider in cascade:
        keys = _provider_keys(provider["key_env"])
        if not keys:
            print(f"  skip {provider['name']}: {provider['key_env']} not set")
            continue
        if needs_vision and not provider.get("vision", True):
            msg = f"{provider['name']}: skipped (no vision, request has image parts)"
            print(f"  skip {msg}")
            errors.append(msg)
            continue
        if left() <= 0:
            msg = f"{provider['name']}: skipped, {budget_s:.0f}s cascade budget exhausted"
            print(f"  ! {msg}")
            errors.append(msg)
            continue

        if messages is not None:
            msgs = _messages_with_schema_instruction(
                messages, schema, provider["structured"] == "json_object")
        else:
            msgs = []
            sys_content = system or ""
            if schema and provider["structured"] == "json_object":
                sys_content += (
                    "\n\nRespond ONLY with a single valid JSON object matching "
                    "this JSON Schema exactly:\n" + json.dumps(schema)
                )
            if sys_content:
                msgs.append({"role": "system", "content": sys_content})
            msgs.append({"role": "user", "content": prompt})

        payload = {
            "model": provider["model"],
            "messages": msgs,
            "temperature": temperature,
        }
        # Per-entry extra body params (e.g. groq's reasoning_effort for
        # gpt-oss models — without "low" the hidden reasoning eats the
        # whole max_tokens budget and content comes back empty).
        payload.update(provider.get("extra") or {})
        if schema:
            if provider["structured"] == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True,
                                    "schema": schema},
                }
            else:
                payload["response_format"] = {"type": "json_object"}

        for key_index, api_key in enumerate(keys, 1):
            if left() <= 0:
                msg = f"{provider['name']}: skipped, {budget_s:.0f}s cascade budget exhausted"
                print(f"  ! {msg}")
                errors.append(msg)
                break
            for attempt in range(max_retries_per_provider + 1):
                print(f"-> {provider['name']} ({provider['model']}), "
                      f"key {key_index}/{len(keys)}, attempt {attempt + 1}")
                try:
                    # Never let one call outlive the budget: a provider that streams slowly used
                    # to blow past the 120s socket timeout because that timeout is per read, not
                    # total. Also cap each ATTEMPT well below the cascade budget: free-tier
                    # providers (observed Aug 2026 on Gemini flash-lite) sometimes queue a
                    # generation for 60-90s+ instead of answering — failing that attempt fast
                    # and moving to the next cascade entry beats hanging the client.
                    text = _post_chat(provider["url"], api_key, payload,
                                      timeout=max(1, min(_attempt_timeout_s(), left())))
                    if schema:
                        # Some models wrap JSON in markdown fences; strip them.
                        cleaned = text.strip()
                        if cleaned.startswith("```"):
                            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
                        return json.loads(cleaned), provider["name"]
                    return text, provider["name"]
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode("utf-8")[:300]
                    except Exception:
                        pass
                    msg = f"{provider['name']} HTTP {e.code}: {detail}"
                    print(f"  ! {msg}")
                    errors.append(msg)
                    if e.code == 429 and _NO_RETRY_CAP.search(detail):
                        print("    daily allowance or spent credits, not a burst limit -> next key")
                        break
                    if e.code == 429 and attempt < max_retries_per_provider and left() > 5:
                        time.sleep(5)
                        continue
                    break  # non-retryable, retries exhausted, or no budget -> next key
                except Exception as e:  # timeouts, bad JSON, network errors
                    msg = f"{provider['name']}: {type(e).__name__}: {e}"
                    print(f"  ! {msg}")
                    errors.append(msg)
                    break

    raise RuntimeError(
        "All providers in the cascade failed or were skipped:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


if __name__ == "__main__":
    # Smoke test: python3 gateway.py "your prompt"
    result, used = complete(sys.argv[1] if len(sys.argv) > 1 else "Say OK.")
    print(f"[{used}]\n{result}")
