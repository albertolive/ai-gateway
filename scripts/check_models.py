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


def rank_candidates(catalog, pinned_ids):
    out = []
    for mid, m in catalog.items():
        if not is_free(m) or mid in pinned_ids or mid == AUTO_ROUTER:
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

    # --update: drop dead entries; replace dead openrouter entries in-place
    # with the best unused candidate so cascade depth is preserved.
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
                    repl = next((c for c in candidates if c["id"] not in used),
                                None)
                    if repl:
                        used.add(repl["id"])
                        new_entries.append({"provider": "openrouter",
                                            "model": repl["id"],
                                            "structured": repl["structured"]})
                        kept_notes.append(
                            f"replaced `{e['model']}` with `{repl['id']}` "
                            f"({death[3]})")
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
    lines.append("| model | score | context | structured outputs |")
    lines.append("|---|---|---|---|")
    for c in candidates[:10]:
        lines.append(f"| `{c['id']}` | {c['score']} | {c['context_length']:,} "
                     f"| {'yes' if c['structured'] == 'json_schema' else 'no'} |")
    lines += ["", "_To promote a candidate, edit `models.json` — scoring: "
              "+2 coding-oriented name, +1 strict structured outputs, "
              "ties broken by context length._"]

    report = "\n".join(lines)
    with open("model_watch_report.md", "w", encoding="utf-8") as f:
        f.write(report)
    print(report)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"attention={'true' if dead else 'false'}\n")


if __name__ == "__main__":
    main()
