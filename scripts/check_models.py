"""Model-watch: detect deprecated / no-longer-free models and new candidates.

Queries each provider's live model catalog, validates every model pinned in
models.json, ranks new free candidates, and writes model_watch_report.md.
With --update it also rewrites models.json: dead models are removed and — if
a cascade entry died — replaced by the top-ranked candidate. New-model
suggestions are report-only (a human promotes them by merging the bot PR or
editing models.json).

Catalog endpoints (all verified July 2026):
- OpenRouter: GET /api/v1/models (public, no key). Free = ":free" suffix or
  pricing.prompt == pricing.completion == 0. Includes context_length and
  supported_parameters (e.g. "structured_outputs").
- Google:     GET /v1beta/models/{model}?key=K -> 200 exists / 404 gone.
- Groq:       GET /openai/v1/models/{id} with Bearer key -> 200 / 404.

Exit code is always 0 unless the OpenRouter catalog itself is unreachable;
"changed"/"attention" flags go to GITHUB_OUTPUT for the workflow.

Env: OPENROUTER_API_KEY (optional), GEMINI_API_KEY (optional),
     GROQ_API_KEY (optional), GITHUB_OUTPUT (set by Actions).
"""

import json
import os
import sys
import urllib.error
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "models.json")
CODING_HINTS = ("coder", "code", "laguna", "codestral", "devstral", "starcoder")
# Never auto-remove these: the auto-router is the safety net, and non-OpenRouter
# entries are validated separately.
AUTO_ROUTER = "openrouter/free"
# Scored well here (catalog metadata: coding-name and/or structured_outputs)
# but disproven live against a real prompt -- excluded so it stops resurfacing
# as a "promising candidate" every week. Add the finding, not just the ID.
KNOWN_UNSUITABLE = {
    "nvidia/nemotron-3-super-120b-a12b:free":
        "leaks raw chain-of-thought into message.content under a real "
        "constrained prompt (verified live, July 2026, esdeveniments-"
        "social-publisher's caption generation) -- a reasoning-hybrid model "
        "putting reasoning where the output should be.",
}


def _get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def fetch_openrouter_catalog():
    headers = {}
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    _, body = _get("https://openrouter.ai/api/v1/models", headers)
    return {m["id"]: m for m in json.loads(body).get("data", [])}


def is_free(m):
    # Pricing is the ground truth; the ":free" suffix is only a fallback
    # when pricing data is missing (a ":free" id with nonzero price is paid).
    pricing = m.get("pricing") or {}
    try:
        return float(pricing["prompt"]) == 0 and \
               float(pricing["completion"]) == 0
    except (KeyError, TypeError, ValueError):
        return m["id"].endswith(":free")


def model_exists(provider, model):
    """Existence probe for gemini/groq. Returns True/False/None (unknown).

    Known gap (found live, July 2026): this only checks that the model ID
    resolves (200 on /v1beta/models/{id}) — it does NOT catch a model whose
    ID is still valid but has been deprecated for new free-tier quota
    allocation (0 RPM/TPM/RPD in AI Studio's rate-limit table). That's how
    gemini-2.0-flash went undetected here while returning 429 on every real
    call. A true fix needs a live completions call, not just an existence
    check — not implemented, flagging for next iteration.
    """
    try:
        if provider == "gemini":
            key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not key:
                return None
            status, _ = _get("https://generativelanguage.googleapis.com/"
                             f"v1beta/models/{model}?key={key}")
        elif provider == "groq":
            key = os.environ.get("GROQ_API_KEY", "").strip()
            if not key:
                return None
            status, _ = _get(f"https://api.groq.com/openai/v1/models/{model}",
                             {"Authorization": f"Bearer {key}"})
        else:
            return None
        return status == 200
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:
        return None  # network issue: don't declare a model dead on a hiccup


# A moderately-constrained prompt, not a trivial "reply OK" — that's exactly
# what let nvidia/nemotron-3-super-120b-a12b:free look fine in a quick check
# while failing on real usage (see KNOWN_UNSUITABLE). Deterministic answer so
# pass/fail doesn't need a judge model: exact values are easy to check by hand.
SMOKE_TEST_PROMPT = (
    "Rules: reply with a JSON object matching {\"greeting\": string, \"count\": "
    "integer}. greeting must be exactly \"hello\". count must be exactly 3. "
    "Respond with ONLY the JSON object, no explanation, no markdown."
)
# If the response opens with one of these before any JSON, that's the model
# narrating its reasoning into the answer instead of just answering --
# the exact failure mode found live in nemotron-3-super-120b-a12b.
REASONING_LEAK_PREFIXES = (
    "we need", "let me", "i need to", "i should", "okay,", "ok,", "first,",
    "the user wants", "thinking",
)


def _post_chat_completion(model_id, structured, api_key, timeout=30):
    """POST one chat/completions call to OpenRouter. Returns the raw content string.

    Split out from smoke_test() so tests can monkeypatch just the network
    call, matching gateway.py's _post_chat pattern.
    """
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": SMOKE_TEST_PROMPT}],
        "temperature": 0.1,
    }
    if structured == "json_schema":
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "smoke_test", "strict": True, "schema": {
                "type": "object",
                "properties": {"greeting": {"type": "string"},
                                "count": {"type": "integer"}},
                "required": ["greeting", "count"], "additionalProperties": False,
            }},
        }
    else:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


def smoke_test(model_id, structured, api_key, timeout=30):
    """Run one real call against `model_id`. Returns (passed, reason).

    Catches gross failures a catalog score can't see: empty responses,
    reasoning leaked into content instead of the answer, malformed JSON,
    or the model simply not following an explicit instruction. Does NOT
    judge writing quality/tone -- that still needs a human look before
    promoting.
    """
    try:
        content = _post_chat_completion(model_id, structured, api_key, timeout)
    except Exception as e:
        return False, f"request failed: {e}"

    if not content or not content.strip():
        return False, "empty response"

    stripped = content.strip()
    if any(stripped.lower().startswith(p) for p in REASONING_LEAK_PREFIXES):
        return False, f"reasoning leaked into content: {stripped[:80]!r}"

    cleaned = stripped
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0] if "\n" in cleaned else cleaned

    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        return False, f"invalid JSON: {e} — raw: {stripped[:80]!r}"

    if parsed.get("greeting") != "hello" or parsed.get("count") != 3:
        return False, f"ignored instructions: got {parsed!r}"

    return True, "ok"


def rank_candidates(catalog, pinned_ids):
    out = []
    for mid, m in catalog.items():
        if not is_free(m) or mid in pinned_ids or mid == AUTO_ROUTER or mid in KNOWN_UNSUITABLE:
            continue
        params = m.get("supported_parameters", []) or []
        score = 0
        if any(h in mid.lower() for h in CODING_HINTS):
            score += 2
        if "structured_outputs" in params:
            score += 1
        out.append({
            "id": mid, "score": score,
            "context_length": m.get("context_length") or 0,
            "structured": "json_schema" if "structured_outputs" in params
                          else "json_object",
        })
    out.sort(key=lambda c: (c["score"], c["context_length"]), reverse=True)
    return out


def main():
    update = "--update" in sys.argv
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    cascades = config["cascades"]

    try:
        catalog = fetch_openrouter_catalog()
    except Exception as e:
        print(f"FATAL: cannot fetch OpenRouter catalog: {e}")
        sys.exit(1)

    pinned_or = {e["model"] for c in cascades.values() for e in c
                 if e["provider"] == "openrouter"}
    dead, unknown, kept_notes = [], [], []

    for intent, entries in cascades.items():
        for e in entries:
            pid, model = e["provider"], e["model"]
            if pid == "openrouter":
                if model == AUTO_ROUTER:
                    continue
                m = catalog.get(model)
                if m is None:
                    dead.append((intent, pid, model, "gone from catalog"))
                elif not is_free(m):
                    dead.append((intent, pid, model, "no longer free"))
            else:
                exists = model_exists(pid, model)
                if exists is False:
                    dead.append((intent, pid, model, "404 from provider"))
                elif exists is None:
                    unknown.append((intent, pid, model,
                                    "not checked (no key or network error)"))

    candidates = rank_candidates(catalog, pinned_or)

    # Smoke-test only the top few (real API calls cost quota) -- a score is a
    # catalog-metadata guess, this is the closest thing to ground truth we
    # have without a human reading the output. Skipped entirely with no key
    # (report/replacement fall back to score-only, matching prior behavior).
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key:
        for c in candidates[:3]:
            passed, reason = smoke_test(c["id"], c["structured"], or_key)
            c["smoke_test"], c["smoke_test_reason"] = passed, reason

    # --update: drop dead entries; replace dead openrouter entries in-place
    # with the best unused candidate so cascade depth is preserved. Prefers a
    # smoke-tested-passing candidate over a purely rank-ordered one -- a live
    # cascade replacement is a higher-stakes pick than a report suggestion.
    changed = False
    if update and dead:
        used = set(pinned_or)
        for intent, entries in cascades.items():
            new_entries = []
            for e in entries:
                key = (intent, e["provider"], e["model"])
                death = next((d for d in dead if d[:3] == key), None)
                if death is None:
                    new_entries.append(e)
                    continue
                changed = True
                if e["provider"] == "openrouter":
                    repl = next((c for c in candidates
                                 if c["id"] not in used and c.get("smoke_test") is True),
                                None) or next((c for c in candidates if c["id"] not in used),
                                None)
                    if repl:
                        used.add(repl["id"])
                        new_entries.append({"provider": "openrouter",
                                            "model": repl["id"],
                                            "structured": repl["structured"]})
                        smoke_note = ("smoke-tested OK" if repl.get("smoke_test") is True
                                     else "NOT smoke-tested — verify before trusting")
                        kept_notes.append(
                            f"replaced `{e['model']}` with `{repl['id']}` "
                            f"({death[3]}, {smoke_note})")
                    else:
                        kept_notes.append(f"removed `{e['model']}` ({death[3]}), "
                                          "no candidate available")
                else:
                    kept_notes.append(f"removed `{e['model']}` ({death[3]}) — "
                                      "pick a replacement manually")
            cascades[intent] = new_entries
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")

    # ---- report ----
    lines = ["# Model watch report", ""]
    lines.append(f"Free OpenRouter models live right now: **{sum(1 for m in catalog.values() if is_free(m))}**")
    lines.append("")
    if dead:
        lines.append("## Deprecated / no longer free (action taken)" if update
                     else "## Deprecated / no longer free")
        lines += [f"- `{m}` ({p}, cascade `{i}`): {why}"
                  for i, p, m, why in dead]
        lines += [""] + [f"- {n}" for n in kept_notes] + [""]
    else:
        lines += ["## Pinned models", "", "All pinned models are alive and free.", ""]
    if unknown:
        lines.append("## Not verifiable")
        lines += [f"- `{m}` ({p}, cascade `{i}`): {why}"
                  for i, p, m, why in unknown] + [""]
    lines.append("## Top new free candidates (not pinned)")
    lines.append("")
    lines.append("| model | score | context | structured outputs | smoke test |")
    lines.append("|---|---|---|---|---|")
    for c in candidates[:10]:
        if "smoke_test" not in c:
            smoke_col = "not tested"
        elif c["smoke_test"]:
            smoke_col = "✅ passed"
        else:
            smoke_col = f"❌ {c['smoke_test_reason']}"
        lines.append(f"| `{c['id']}` | {c['score']} | {c['context_length']:,} "
                     f"| {'yes' if c['structured'] == 'json_schema' else 'no'} "
                     f"| {smoke_col} |")
    lines += ["", "_To promote a candidate, edit `models.json` — scoring: "
              "+2 coding-oriented name, +1 strict structured outputs, "
              "ties broken by context length. The score is a catalog-metadata "
              "proxy, not a correctness check — it won't catch a model that "
              "leaks reasoning into output or ignores response_format under "
              "real prompts (verified live, July 2026: a high-scoring "
              "coding-named candidate did exactly this). Test the candidate "
              "against a real prompt from an actual cascade user before "
              "promoting, not just this table._"]

    report = "\n".join(lines)
    with open("model_watch_report.md", "w", encoding="utf-8") as f:
        f.write(report)
    print(report)

    # A candidate scoring 0 is just "some free model exists" (true almost every
    # week on a rotating catalog) — not worth a human look. >=1 means it has at
    # least one real signal (coding-oriented name or strict structured outputs).
    # A candidate that was smoke-tested and FAILED is excluded even if it
    # scored well — that's exactly what happened with nemotron; score alone
    # would have kept flagging it as promising forever.
    promising_candidate = any(
        c["score"] >= 1 and c.get("smoke_test") is not False
        for c in candidates[:3]
    )

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"attention={'true' if dead else 'false'}\n")
            f.write(f"promising_candidate={'true' if promising_candidate else 'false'}\n")


if __name__ == "__main__":
    main()
