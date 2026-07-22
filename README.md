# ai-gateway

Centralized, free-tier AI infrastructure for GitHub Actions — a replacement for GitHub Models (retired July 30, 2026). One repo holds the logic; every other repo calls it with a small caller file.

Two reusable workflows, one provider cascade:

- **`pr-review.yml`** — inline AI pull-request reviews (line-anchored comments with committable ` ```suggestion ` blocks, plus a summary), like the retired Gemini Code Assist. **Incremental:** the bot hides the last-reviewed SHA in its summary; on new commits it reviews only what changed since (full re-review after a force-push). **Context-aware:** each review includes the target repo's `.ai-review.md` guidelines, dependency manifests with exact versions, and live docs for the libraries the PR actually imports — resolved **dynamically for any npm package** (see "How docs resolution works" below), so it judges API usage against current docs, not stale training data.
- **`pr-reply.yml`** — conversational mode: reply to the bot inside one of its review threads and it answers in-thread, with the diff hunk and full thread as context. Only responds in threads it started; ignores other bots to prevent loops. Has its own `concurrency` block to cancel duplicate replies from rapid-fire responses. **Learnings memory:** when your reply teaches it a durable rule ("this is intentional, stop flagging it"), it saves the rule to an "AI Review Learnings" issue in the repo and applies it to every future review — the issue is the whole database, so you can read, edit, or delete learnings by editing the issue.
- **Static analysis layer** (`scripts/lint.py`) — deterministic findings feed the review: ruff for Python, shellcheck for shell (both skipped gracefully if absent), plus a built-in secrets scan (GitHub/AWS/Google/OpenAI/Slack key patterns and generic hardcoded credentials) that always runs. Findings on changed lines are handed to the model to validate and fold into its comments — the same "analyzers + LLM" pattern CodeRabbit uses, minus ~22 tools.
- **Cross-file impact analysis** (`scripts/impact.py`) — closes the biggest quality gap vs. commercial repo-indexing reviewers (CodeRabbit, Greptile). When the PR checkout is available, the reviewer extracts symbols (functions, classes, constants) that were removed, renamed, or had their definitions modified in the diff, then scans the rest of the repo for references to those symbols in files **not** in the PR. This catches the #1 cross-file bug class — "I renamed `fetchData` in `api.js` but `app.js` still calls the old name" — at $0 with no infrastructure. It's not a full code graph (no type inference, no call chains), but it covers the 80% case. Pure stdlib, size-capped (max 10 symbols, 15 refs total), and dunder methods + noise words are filtered.
- **`llm-task.yml`** — generic LLM tasks (release notes, triage, summaries). Pass a prompt, get the result back as a job output.

The cascade (in `scripts/gateway.py`) tries free providers in order and fails over automatically on rate limits or outages. Providers without a configured key are skipped, so any subset of keys works.

**`code_review` cascade** (used by PR reviews and thread replies):

| Order | Provider | Model | Free limits (July 2026) |
|---|---|---|---|
| 1 | OpenRouter | `cohere/north-mini-code:free` | ~20 req/min, ~200 req/day (shared pool) |
| 2 | OpenRouter | `poolside/laguna-m.1:free` | same shared pool |
| 3 | Google AI Studio | `gemini-2.0-flash` | 15 RPM, 1,500 req/day (check your project's live quotas in AI Studio) |
| 4 | Groq | `llama-3.3-70b-versatile` | 30 RPM, ~1K req/day |
| 5 | OpenRouter | `openrouter/free` (auto-router) | shared pool — non-deterministic, safety net only |

**`general` cascade** (used by `llm-task.yml`): Gemini 2.0 Flash first (stable, high context), then Groq, then `openrouter/free` as the safety net.

> **Model IDs are verified, not assumed.** All IDs above were checked against live provider catalogs on July 22, 2026. `cohere/north-mini-code:free` and `poolside/laguna-m.1:free` are confirmed present in the OpenRouter catalog; `gemini-2.0-flash` is the current valid Flash model on Google's OpenAI-compatible endpoint (no `google/` prefix). The model-watch workflow keeps this current automatically (see below).

The cascade order lives in **`models.json`** (not code), and a third workflow keeps it current:

- **`model-watch.yml`** — every Monday (plus on demand) it queries the live provider catalogs, and if a pinned model was removed or stopped being free, it swaps in the best-ranked replacement and opens a bot PR with a full report — including new free models worth promoting. You stay current by merging a PR, not by reading changelogs.

Scripts are **stdlib-only Python** — no `pip install`, no supply-chain surface, faster CI.

## Setup

1. **Create the repo.** Push this directory to `github.com/<you>/ai-gateway`. Public is simplest. If private: Settings → Actions → General → Access → *Accessible from repositories owned by \<you/org\>*.
2. **Replace the placeholder.** Search for `YOUR_GITHUB_USERNAME_OR_ORG` in `.github/workflows/*.yml`, `caller-templates/*.yml`, and `deploy-callers.sh`. Also, in all three reusable workflows (`pr-review.yml`, `pr-reply.yml`, `llm-task.yml`), change the gateway checkout `ref: main` to `ref: v1.0.0` (or your tagged release) so scripts are pinned along with the workflow — **never deploy with `ref: main`, it's the exact supply-chain anti-pattern this tool is designed to avoid**.
3. **Get free API keys** (no card needed for any):
   - OpenRouter: https://openrouter.ai/keys
   - Google AI Studio: https://aistudio.google.com/apikey
   - Groq: https://console.groq.com/keys
4. **Store the secrets** as `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`:
   - **Org repos:** Organization Settings → Secrets and variables → Actions → org-level secrets, scoped to the repos you want. Set once, done.
   - **Personal repos:** no account-level secrets exist, so set per repo: `gh secret set OPENROUTER_API_KEY -R you/repo --body "sk-or-..."` (loop over repos, or let `deploy-callers.sh` remind you).
5. **Tag a release.** Callers must pin a tag, never `@main`:
   ```bash
   git tag v1.0.0 && git push origin v1.0.0
   ```
   Then verify all three reusable workflows use `ref: v1.0.0` in their gateway checkout step (step 2 covers this).
6. **Add `.gitignore`.** The repo includes a `.gitignore` that excludes `__pycache__/`, generated CI outputs (`model_watch_report.md`, `pr_diff.txt`, `gateway_output.*`, etc.), and secret files. Don't track these — `model_watch_report.md` is a generated output of `check_models.py`, not a source file. If it's already tracked in a remote, untrack it with `git rm --cached model_watch_report.md`.
7. **Enable model-watch bot PRs** (one-time, in the `ai-gateway` repo): Settings → Actions → General → Workflow permissions → check *Allow GitHub Actions to create and approve pull requests*. Optionally add the three API keys as repo secrets here too, so the watcher can also verify the Gemini and Groq models (it verifies OpenRouter without any key).
8. **Deploy callers.** Copy `caller-templates/ai-review.yml` into each repo's `.github/workflows/`, or edit the repo list in `deploy-callers.sh` and run it (needs `gh auth refresh -s workflow`).

## Testing

The project has a test suite (106 tests) covering the diff parser, comment validator, impact analysis, context/docs resolution, lint/secrets scan, gateway cascade loading, model-watch ranking, and learnings memory logic. Tests are pure-logic (no network calls) and use pytest.

```bash
python3 -m pytest tests/ -v
```

Tests live in `tests/` with a shared `conftest.py` that adds `scripts/` to the path. Run them locally before pushing, and consider adding a CI step to run them on every push to the gateway repo.

## Upgrading

Edit centrally, tag `v1.1.0`, then bump the tag in callers when ready. Nothing breaks mid-flight because callers pin tags.

## Customizing

- **Models/providers:** edit `models.json` — no code changes needed. The model-watch bot edits the same file, so your manual picks and its replacements coexist. Provider endpoints/keys are mapped in `PROVIDERS` in `scripts/gateway.py`.
- **Model-watch behavior:** deprecation handling is automatic (dead models replaced in the bot PR); promotion of *new* models is deliberate (they're ranked in the report — +2 coding-oriented name, +1 strict structured outputs, tie-break by context length — and you promote one by editing `models.json`). The `openrouter/free` auto-router entry is a permanent safety net: even between weekly runs, a dead pinned model just fails over at runtime.
- **Review behavior:** edit `SYSTEM_PROMPT` in `scripts/review.py`. Comment cap (10) and diff-size cap (`MAX_DIFF_CHARS`, 200 KB) are constants there.
- **Per-repo guidelines:** add a `.ai-review.md` to any target repo — conventions to enforce, patterns to ignore, domain context. It's prepended to every review of that repo.
- **How docs resolution works (dynamic — any library):** `context.py` first extracts package names from `import`/`require` statements in the diff and intersects them with `package.json`, so docs are fetched only for libraries the PR actually touches (max 3 per review). Each package is then resolved through three tiers, first hit wins: (1) a manual override in `docs_sources.json` — for non-standard paths or internal libraries; (2) fully dynamic discovery — the npm registry provides the package's homepage, and `<homepage>/llms.txt` and `<homepage>/docs/llms.txt` are probed (the `llms.txt` standard that React, Next.js, Svelte, Vercel and a growing list of projects publish); (3) optionally the **Context7 API** (`context7.com`), a maintained docs index covering thousands of libraries — set a `CONTEXT7_API_KEY` secret to enable it, mindful that its free tier is ~1,000 requests/month. Everything is best-effort and size-capped (~30 KB total) to respect free-tier token limits. This mirrors how commercial tools work: CodeRabbit performs live web queries per review rather than pre-indexing the world; Cursor resolves user-registered docs URLs; Context7 is the shared-registry approach.
- **Cross-file impact analysis:** `impact.py` extracts symbols from removed/modified diff lines using regex patterns for Python, JS/TS, Go, and Rust, then greps the repo (via `os.walk`) for word-boundary references in source files outside the diff. Noise dirs (`node_modules`, `.git`, `__pycache__`, etc.), dunder methods (`__init__`, `__str__`), and common short names (`get`, `set`, `run`) are filtered. Capped at 10 symbols / 15 refs / 2000 files scanned to respect free-tier token limits and CI runner time. Not a full code graph — no type inference or call-chain analysis — but catches the most common cross-file breakage class for free.
- **Incremental caveat:** if a PR merges its base branch in, those merged commits appear in the incremental diff too. The `synchronize` trigger fires on every push; concurrency cancellation keeps only the newest run.
- **Enforcement (orgs):** add a Repository Ruleset requiring the `review` status check on protected branches, so deleting the caller file can't bypass review.

## Design notes (why it's built this way)

- **No line-number math by the model.** `review.py` pre-parses the diff and annotates every line as `path::line::[ADDED]::`; the model copies numbers, and every comment is validated against the real diff before posting. Invalid targets are dropped, and if GitHub still rejects the inline review, it falls back to a single summary comment.
- **Strict structured outputs** via `response_format: {type: "json_schema", strict: true}` where supported (OpenRouter, Gemini); JSON-object mode with the schema embedded in the prompt on Groq.
- **Security:** actions pinned to commit SHAs, least-privilege `permissions`, `persist-credentials: false`, no untrusted PR text interpolated into `run:` blocks, `concurrency` cancels superseded runs.
- **Why not ZeroLimitAI / scraped free-model routers:** fine for personal scripts, but in CI you need determinism, a privacy policy that covers your code, and an endpoint that won't vanish mid-build. Direct free tiers from OpenRouter / Google / Groq provide that; the cascade covers their individual flakiness.

## Limits to keep in mind

- OpenRouter's ~200 free requests/day is shared across all your repos' PRs. Busy fleet → traffic spills to Gemini/Groq automatically, but a very busy day can exhaust everything. That's the price of $0. For a fleet of 90+ repos, consider running your own local model (Ollama) as an additional cascade tier — see `PROVIDERS` in `scripts/gateway.py`.
- `gemini-2.0-flash` free-tier prompts may be used by Google for training — fine for open source, think twice for proprietary code (OpenRouter free models have similar caveats; read each provider's data policy).
- The review job soft-fails into a summary comment, but if all providers are down the check fails. Don't make it a *required* check unless you accept occasional re-runs.
