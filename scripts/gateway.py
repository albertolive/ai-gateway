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
            })
    return resolved


CASCADES = load_cascades()


def _post_chat(base_url, api_key, payload, timeout=120):
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional, ignored by others)
            "HTTP-Referer": "https://github.com",
            "X-Title": "ai-gateway",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def complete(prompt, system=None, intent="general", schema=None,
             schema_name="response", temperature=0.1, max_retries_per_provider=1):
    """Run prompt through the cascade. Returns (text, provider_name).

    If `schema` (a JSON Schema dict) is given, output is requested/validated
    as JSON and the parsed object is returned instead of raw text.
    """
    cascade = CASCADES.get(intent, CASCADES["general"])
    errors = []

    for provider in cascade:
        api_key = os.environ.get(provider["key_env"], "").strip()
        if not api_key:
            print(f"  skip {provider['name']}: {provider['key_env']} not set")
            continue

        messages = []
        sys_content = system or ""
        if schema and provider["structured"] == "json_object":
            sys_content += (
                "\n\nRespond ONLY with a single valid JSON object matching "
                "this JSON Schema exactly:\n" + json.dumps(schema)
            )
        if sys_content:
            messages.append({"role": "system", "content": sys_content})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
        }
        if schema:
            if provider["structured"] == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True,
                                    "schema": schema},
                }
            else:
                payload["response_format"] = {"type": "json_object"}

        for attempt in range(max_retries_per_provider + 1):
            print(f"-> {provider['name']} ({provider['model']}), attempt {attempt + 1}")
            try:
                text = _post_chat(provider["url"], api_key, payload)
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
                if e.code == 429 and attempt < max_retries_per_provider:
                    time.sleep(5)
                    continue
                break  # non-retryable or retries exhausted -> next provider
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
