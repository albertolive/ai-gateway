"""Gather review context from the target repo + fresh docs, dynamically.

How docs are resolved for ANY dependency (no hardcoded framework list):
1. Relevance: package names are extracted from import/require statements
   in the diff itself, then intersected with package.json — docs are only
   fetched for libraries the PR actually touches.
2. Resolution, per package, first hit wins:
   a. docs_sources.json manual override (always wins)
   b. npm registry -> package homepage -> probe <origin>/llms.txt and
      <origin>/docs/llms.txt (the emerging standard; React, Next.js,
      Svelte, AI SDK etc. publish these)
   c. Context7 API (context7.com), only if CONTEXT7_API_KEY is set —
      covers thousands of libraries that don't publish llms.txt.
      Free tier is ~1,000 req/month, so it's an opt-in fallback.
3. Everything is size-capped and best-effort: a failed fetch never fails
   the review, it just means less context.

Also gathered: `.ai-review.md` guidelines and dependency manifests.
"""

import json
import os
import re
import urllib.parse
import urllib.request

DOCS_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "docs_sources.json")
GUIDELINES_FILE = ".ai-review.md"
MANIFEST_FILES = ("package.json", "pyproject.toml", "requirements.txt",
                  "go.mod", "Cargo.toml", "composer.json", "Gemfile")

CAP_GUIDELINES = 8000
CAP_MANIFEST = 4000
CAP_DOC = 12000
CAP_DOCS_TOTAL = 30000
MAX_DOC_SOURCES = 3

LLMS_TXT_PATHS = ("/llms.txt", "/docs/llms.txt")

IMPORT_RE = re.compile(
    r"""(?:from\s+|import\s+|require\(\s*)['"]([a-zA-Z@][\w@/.-]*)['"]""")


def _read_capped(path, cap):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(cap + 1)
        return text[:cap] + ("\n[truncated]" if len(text) > cap else "")
    except OSError:
        return None


def _http_get(url, cap, headers=None, timeout=20):
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ai-gateway", **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(cap + 1).decode("utf-8", errors="replace")[:cap]
    except Exception:
        return None


def _js_deps(target_dir):
    try:
        with open(os.path.join(target_dir, "package.json"),
                  encoding="utf-8") as f:
            pkg = json.load(f)
        deps = {}
        for section in ("dependencies", "devDependencies"):
            deps.update(pkg.get(section) or {})
        return deps
    except (OSError, ValueError):
        return {}


def packages_in_diff(diff_text, deps):
    """Package names imported in the diff that are real dependencies."""
    found = []
    for spec in IMPORT_RE.findall(diff_text or ""):
        if spec.startswith("."):
            continue  # relative import
        parts = spec.split("/")
        pkg = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
        if pkg in deps and pkg not in found:
            found.append(pkg)
    return found


def _resolve_via_llms_txt(pkg):
    """npm registry -> homepage -> probe llms.txt conventions."""
    meta = _http_get("https://registry.npmjs.org/"
                     f"{urllib.parse.quote(pkg, safe='@/')}/latest", 200000)
    if not meta:
        return None
    try:
        homepage = json.loads(meta).get("homepage") or ""
    except ValueError:
        return None
    parsed = urllib.parse.urlparse(homepage)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if parsed.netloc.endswith("github.com"):
        return None  # repo pages don't serve llms.txt
    origin = f"https://{parsed.netloc}"
    for path in LLMS_TXT_PATHS:
        text = _http_get(origin + path, CAP_DOC)
        if text and text.strip() and not text.lstrip().startswith("<"):
            return (origin + path, text)
    return None


def _resolve_via_context7(pkg, query):
    """Optional deep fallback: Context7's maintained docs index."""
    key = os.environ.get("CONTEXT7_API_KEY", "").strip()
    if not key:
        return None
    headers = {"Authorization": f"Bearer {key}"}
    search = _http_get("https://context7.com/api/v2/libs/search?query="
                       + urllib.parse.quote(pkg), 50000, headers)
    if not search:
        return None
    try:
        results = json.loads(search).get("results") or []
        lib_id = results[0]["id"]
    except (ValueError, KeyError, IndexError):
        return None
    ctx = _http_get(
        "https://context7.com/api/v2/context?libraryId="
        + urllib.parse.quote(str(lib_id), safe="")
        + "&query=" + urllib.parse.quote(query or pkg), CAP_DOC, headers)
    if ctx and ctx.strip():
        return (f"context7:{lib_id}", ctx)
    return None


def resolve_docs(pkg, query=""):
    """Return (source_label, text) or None. Override > llms.txt > Context7."""
    try:
        with open(DOCS_CONFIG, encoding="utf-8") as f:
            overrides = {k: v for k, v in json.load(f).items()
                         if not k.startswith("_")}
    except (OSError, ValueError):
        overrides = {}
    if pkg in overrides:
        text = _http_get(overrides[pkg], CAP_DOC)
        if text and text.strip():
            return (overrides[pkg], text)
    return _resolve_via_llms_txt(pkg) or _resolve_via_context7(pkg, query)


def gather(target_dir, diff_text=""):
    """Return a markdown context block (may be empty string)."""
    sections = []

    guidelines = _read_capped(os.path.join(target_dir, GUIDELINES_FILE),
                              CAP_GUIDELINES)
    if guidelines:
        sections.append("## Repository review guidelines (follow these)\n\n"
                        + guidelines)

    manifests = []
    for name in MANIFEST_FILES:
        text = _read_capped(os.path.join(target_dir, name), CAP_MANIFEST)
        if text:
            manifests.append(f"### {name}\n```\n{text}\n```")
    if manifests:
        sections.append("## Project dependencies (exact versions — judge "
                        "API usage against these, not your assumptions)\n\n"
                        + "\n\n".join(manifests))

    deps = _js_deps(target_dir)
    docs, total = [], 0
    for pkg in packages_in_diff(diff_text, deps)[:MAX_DOC_SOURCES]:
        if total >= CAP_DOCS_TOTAL:
            break
        resolved = resolve_docs(pkg, query=f"{pkg} API usage")
        if resolved:
            source, text = resolved
            text = text[:CAP_DOCS_TOTAL - total]
            total += len(text)
            docs.append(f"### Current `{pkg}` ({deps.get(pkg, '?')}) docs "
                        f"— source: {source}\n{text}")
            print(f"  context: docs for '{pkg}' via {source} ({len(text)} chars)")
        else:
            print(f"  context: no docs source found for '{pkg}'")
    if docs:
        sections.append("## Up-to-date library documentation (fetched live "
                        "for packages this PR imports)\n\n"
                        + "\n\n".join(docs))

    return "\n\n".join(sections)
