"""Vendored ai-gateway client for application runtime code (not CI).

Copy this single file into your app. It fetches the shared provider/model
config from ai-gateway's models.json on each call, so swapping a model or
adding a provider (e.g. a paid one like DeepSeek) is a one-line edit in
ai-gateway — this file and your app code never change. If the fetch fails
(GitHub outage), it falls back to OpenRouter's free auto-router so the app
doesn't hard-fail on a transient network issue.

Usage:
    from gateway_client import complete
    text = complete("Summarize this weather forecast: ...")
    text = complete("...", cascade="deepseek_cheap")  # paid, needs DEEPSEEK_API_KEY

Env: one API key per provider you use (OPENROUTER_API_KEY, DEEPSEEK_API_KEY,
     GEMINI_API_KEY, GROQ_API_KEY). Only providers with a key set are tried.
"""

import json
import os
import urllib.request

CONFIG_URL = "https://raw.githubusercontent.com/albertolive/ai-gateway/main/models.json"

_FALLBACK_PROVIDERS = {"openrouter": {"url": "https://openrouter.ai/api/v1",
                                       "key_env": "OPENROUTER_API_KEY"}}
_FALLBACK_ENTRIES = [{"provider": "openrouter", "model": "openrouter/free"}]


def _load_config(timeout=10):
    try:
        with urllib.request.urlopen(CONFIG_URL, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {"providers": _FALLBACK_PROVIDERS, "cascades": {}}


def complete(prompt, cascade="general", system=None, temperature=0.1, timeout=120):
    """Run prompt through the named cascade from ai-gateway's models.json.

    Tries each entry in order, skipping providers with no API key configured.
    Raises RuntimeError if every entry fails or is unconfigured.
    """
    config = _load_config()
    providers = config.get("providers") or _FALLBACK_PROVIDERS
    entries = config.get("cascades", {}).get(cascade) or _FALLBACK_ENTRIES

    errors = []
    for entry in entries:
        provider = providers.get(entry["provider"])
        if not provider:
            continue
        api_key = os.environ.get(provider["key_env"], "").strip()
        if not api_key:
            continue

        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        req = urllib.request.Request(
            f"{provider['url'].rstrip('/')}/chat/completions",
            data=json.dumps({"model": entry["model"], "messages": messages,
                             "temperature": temperature}).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"]
        except Exception as e:
            errors.append(f"{entry['provider']}/{entry['model']}: {e}")

    raise RuntimeError(
        "All providers in cascade '" + cascade + "' failed or were "
        "unconfigured:\n" + "\n".join(f"  - {e}" for e in errors)
    )


if __name__ == "__main__":
    import sys
    print(complete(sys.argv[1] if len(sys.argv) > 1 else "Say OK."))
