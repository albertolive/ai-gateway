# AI Gateway architecture

## Goal

One place to add, remove, or swap a model or provider (free or paid), and every
project that uses AI follows automatically — with no per-repo edits.

This exists because of a real failure: GitHub Models retired on 2026-07-30 and
every repo that had hardcoded its endpoint/model/key had to be touched by hand.
The gateway exists so that never happens again.

## The single principle

A repo names an **intent** (a cascade), never a provider, model ID, or key.

```
complete(prompt, cascade="general")     # runtime client
POST /api/chat/completions {model:"general"}   # hosted endpoint
```

Everything below that line — which provider, which model, which key, retries,
failover, quota handling — is the gateway's job. Repos that hardcode a provider
or model ID are a bug; a CI check should fail them (Phase 3).

## The two tiers

The gateway centralizes **config and failover logic**. It does not require
routing every call through a running server. There are two delivery modes, both
backed by the same `models.json`:

| | Tier A — client | Tier B — hosted endpoint |
|---|---|---|
| What | Vendored client (`app-callers/`) reads `models.json` and calls providers directly with the repo's own keys | A small OpenAI-compatible service holds all keys + `models.json`; repos call it with one gateway key |
| Keys | Distributed per repo (`set-secrets.sh`) | Central (one place) |
| Model swap | 1 edit in `models.json` | 1 edit in `models.json` |
| Provider/key change | Edit + distribute key | Edit gateway only |
| Latency | No extra hop | One extra hop |
| Infra | None | One serverless function |

**Decision: Tier B is the end-state** ("centralize as much as possible").
Tier A remains for repos that already vendor the client and for CI (GitHub
Actions workflows, which are effectively a hosted gateway already).

## Hosted gateway (Tier B) — on Vercel Hobby

The endpoint is a single stdlib Python function at `api/chat/completions.py`
(reuses `scripts/gateway.py` unchanged), deployed to Vercel's free Hobby tier.

Why Vercel Hobby (verified against Vercel docs, 2026-08):

- Python `/api` file-based functions are supported, including the stdlib
  `BaseHTTPRequestHandler` pattern — no FastAPI/Flask, no supply-chain surface.
- Hobby max function duration is **300s (5 min)**, 2 GB RAM, 4.5 MB payload,
  auto-scaling. Enough for the cascade re-tuned below.
- Deployment is `git push` → Vercel auto-deploy, so "edit `models.json` and
  push" is the whole release process.

### Contract

- `POST /api/chat/completions`, OpenAI chat-completions shape.
- `model` field = **cascade name** (`general`, `code_review`, `creative`,
  `frontier`, `deepseek_cheap`, …). Unknown cascade → `400` listing valid names.
- `messages` = OpenAI-shaped turns; content may be a string **or an array of
  parts** — `image_url` parts pass through unchanged (vision). Cascade entries
  flagged `vision: false` in `models.json` are skipped for image requests.
- Structured outputs: `response_format: {type: json_schema, json_schema: {name,
  schema}}` (strict per-provider where supported) or a top-level `schema` for
  loose `json_object` mode; `content` in the response is then a JSON string.
- Auth: `Authorization: Bearer $GATEWAY_TOKEN`.
- Success: `200` with `choices[0].message.content` and `model` = the provider
  that actually served the request (for observability).
- Failure: `401` bad token, `400` bad body / unknown cascade, `502` all
  providers failed.

### Environment (all held in the Vercel project, never in repos)

| Variable | Purpose |
|---|---|
| `GATEWAY_TOKEN` | Inbound auth (repos send it as the bearer token) |
| `OPENROUTER_API_KEY` | OpenRouter provider |
| `GEMINI_API_KEY` | Google AI Studio provider |
| `GROQ_API_KEY` | Groq provider |
| `DEEPSEEK_API_KEY` | DeepSeek provider |
| `AI_GATEWAY_API_KEY` | Vercel AI Gateway provider (comma-separated keys = multi-account failover) |
| `ANTHROPIC_API_KEY` | Anthropic (Claude) direct provider — `frontier` fallback tier |
| `OPENAI_API_KEY` | OpenAI direct provider — `frontier` fallback tier |
| `AI_GATEWAY_BUDGET_S` | Cascade wall-clock budget, capped at 280s in the server |

### Vercel free-tier constraint

Hobby hard-caps a single invocation at 300s. The server therefore caps the
cascade budget at **280s** (CI keeps its measured 900s budget; the runtime path
must fail fast rather than stall a request). Per-provider socket timeouts are
already bounded by the remaining budget in `gateway.py`.

### What it does NOT do yet (deliberately scoped out)

- Streaming responses.
- Persistent usage counter / dashboard — the gateway logs one flat JSON `usage`
  line per request (aggregate with `scripts/usage.py`), but Vercel Hobby log
  retention is short and there's no durable store yet.

(Vision and structured outputs were added 2026-08 — see the HTTP contract above;
meteo-brief's remaining blockers are per-phase model routing and Langfuse
-style tracing, which are app-level concerns, not endpoint gaps.)

## Rollout

- **Phase 0** — harden the fleet audit (grep the "no runtime AI" repos for
  provider/key references).
- **Phase 1** — enforce the cascade-first contract in every consumer; migrate
  the three drifters (meteo-brief, longevity-dashboard, career-ops) off their
  own provider tables; fix the provider-hardcoding consumers (e.g.
  social-publisher's `select(.provider=="openrouter")`). *Status unverified —
  audit the consumers before trusting this as done.*
- **Phase 2** — **done (2026-08).** The hosted endpoint ships as
  `api/chat/completions.py`; see the contract above. Cutting repos over to a
  single `GATEWAY_TOKEN` is still open — CI callers pass provider keys directly.
- **Phase 3** — write the ADR + add a CI check that fails any repo hardcoding a
  provider/model/key, so the drift cannot return. Still open. The version-pin
  guards in `tests/test_workflows.py::TestGatewayRefPinning` are the same idea
  applied to refs rather than models, and are the template to copy.

## Versioning

One pin, and it is derived rather than written:

| Pin | Value | Why |
|---|---|---|
| Caller → reusable workflow | `@v1` (floating major) | A release reaches the fleet with no per-repo edit |
| Workflow → its own scripts | `${{ job.workflow_sha }}` | The exact commit already running, so it cannot drift |

Each reusable workflow fetches its own `scripts/`, so a literal `ref:` was a
second pin per caller — and it drifted: callers asked for `@v1.3.1`, whose
workflow checked out `ref: v1.2.0`, whose workflow checked out `ref: v1.0.0`.
Twelve repos ran the first release for months. Every pin was individually valid,
so nothing failed. Release: `git tag vX.Y.Z && git tag -f v1 && git push -f
origin v1`. Rollback: `git tag -f v1 <last-good-sha>`.

## Pros / cons (summary)

- **Pros:** model/provider/keys in one place; failover logic written once;
  `model-watch` keeps models current automatically; repos become dumb clients;
  key rotation no longer touches repos.
- **Cons:** a running service (single point of failure, one hop of latency,
  all keys in one place); Hobby's 300s cap forces a fast-fail cascade on the
  runtime path; streaming and structured-output passthrough are not yet built.
