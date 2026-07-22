"""Cross-file impact analysis: find references to changed symbols outside the diff.

Closes the biggest quality gap vs. commercial reviewers (CodeRabbit, Greptile)
that index the whole repo graph. Instead of indexing, we do a targeted scan:
extract symbols (functions, classes, constants) from removed/modified lines
in the diff, then search the rest of the repo for references to those symbols.
This catches the #1 cross-file bug class: "I renamed X in file A but file B
still calls the old name."

Pure stdlib, no subprocess, no external tools. Best-effort and size-capped
to respect free-tier token limits. Runs only when TARGET_DIR is available
(full repo checkout, which the workflow already provides via fetch-depth: 0).
"""

import os
import re

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_SYMBOL_LEN = 3
MAX_SYMBOLS = 10
MAX_REFS_PER_SYMBOL = 5
MAX_TOTAL_REFS = 15
MAX_FILE_SIZE = 500_000
MAX_FILES_SCANNED = 2_000  # safety valve for huge monorepos

# Symbols so common that grepping for them produces pure noise.
_NOISE_SYMBOLS = frozenset({
    "get", "set", "add", "new", "run", "use", "log", "err", "req", "res",
    "app", "ctx", "env", "fn", "id", "do", "init", "main", "test",
    "setup", "start", "stop", "open", "close", "call", "apply", "bind",
    "foo", "bar", "baz", "tmp", "key", "val", "obj", "arr", "self",
    "this", "args", "opts",
})

# Definition patterns — each captures the symbol name from a line of code.
# Applied to the content of removed/added diff lines (after stripping the
# leading +/- marker). Order matters: more specific patterns first.
_DEF_PATTERNS = [
    # JS/TS: function foo(, async function foo(, export function foo(
    re.compile(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("),
    # JS/TS: const/let/var foo =, export const foo =
    re.compile(r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*="),
    # Python/Ruby: def foo( or def foo
    re.compile(r"(?:async\s+)?def\s+(\w+)\b"),
    # Python/JS/TS/Rust: class Foo
    re.compile(r"(?:export\s+)?(?:pub\s+)?class\s+(\w+)\b"),
    # Go: func foo( or func (r *Receiver) foo(
    re.compile(r"func\s+(?:\([^)]*\)\s+)?(\w+)\s*\("),
    # Rust: fn foo(, pub fn foo(
    re.compile(r"(?:pub\s+)?fn\s+(\w+)\s*[\(<]"),
    # TypeScript/JS: interface Foo {, export interface Foo {
    re.compile(r"(?:export\s+)?interface\s+(\w+)\b"),
    # TypeScript: type Foo =, type Foo<T> =, export type Foo =
    re.compile(r"(?:export\s+)?type\s+(\w+)(?:<[^>]*>)?\s*="),
]

_SOURCE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".sh", ".bash", ".yml", ".yaml",
    ".rb", ".go", ".rs", ".java", ".kt", ".swift",
    ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".scala",
})

_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", "dist", "build",
    "vendor", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "coverage", "target",
    ".next", ".nuxt", ".output", ".svelte-kit", ".cache",
})


# ---------------------------------------------------------------------------
# Symbol extraction from diff
# ---------------------------------------------------------------------------

def _extract_defs(line_content):
    """Extract a symbol name from a line of code, or None."""
    for pat in _DEF_PATTERNS:
        m = pat.search(line_content)
        if m:
            name = m.group(1)
            # Filter dunder methods (__init__, __str__, etc.) — every class
            # has them, so grepping for them produces pure noise.
            if name.startswith("__") and name.endswith("__"):
                return None
            return name
    return None


def extract_symbols(diff_text):
    """Extract (removed, modified) symbol sets from a unified diff.

    *removed*: defined in '-' lines but not in '+' lines (deleted or renamed).
    *modified*: defined in both '-' and '+' lines (signature change, body
    rewrite — callers may break if the API surface changed).

    Both sets are filtered for minimum length and noise words.
    """
    removed, added = set(), set()

    for line in diff_text.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            name = _extract_defs(line[1:])
            if name and len(name) >= MIN_SYMBOL_LEN \
                    and name.lower() not in _NOISE_SYMBOLS:
                removed.add(name)
        elif line.startswith("+"):
            name = _extract_defs(line[1:])
            if name and len(name) >= MIN_SYMBOL_LEN:
                added.add(name)

    return removed - added, removed & added


# ---------------------------------------------------------------------------
# Diff file extraction (for exclusion)
# ---------------------------------------------------------------------------

def _diff_files(diff_text):
    """All file paths mentioned in the diff (both a/ and b/ sides)."""
    files = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[6:])
        elif line.startswith("--- a/"):
            files.add(line[6:])
    return files


# ---------------------------------------------------------------------------
# Reference search
# ---------------------------------------------------------------------------

def find_references(target_dir, symbols, exclude_files,
                    max_per_symbol=MAX_REFS_PER_SYMBOL,
                    max_total=MAX_TOTAL_REFS):
    """Search files under target_dir for word-boundary references to symbols.

    Skips files in *exclude_files* (relative paths), files in noise
    directories, non-source files, and files larger than MAX_FILE_SIZE.

    Returns ``{symbol: [(relpath, line_num, line_content), ...]}``.
    """
    if not symbols:
        return {}

    refs = {}
    total = 0
    files_scanned = 0
    # Sort symbols for deterministic iteration order (sets are unordered).
    sorted_symbols = sorted(symbols)
    compiled = {}  # cache compiled patterns per symbol
    for sym in sorted_symbols:
        compiled[sym] = re.compile(r"\b" + re.escape(sym) + r"\b")

    for root, dirs, filenames in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        if total >= max_total or files_scanned >= MAX_FILES_SCANNED:
            break

        for fname in filenames:
            if total >= max_total or files_scanned >= MAX_FILES_SCANNED:
                break
            files_scanned += 1
            ext = os.path.splitext(fname)[1]
            if ext not in _SOURCE_EXTS:
                continue

            fpath = os.path.join(root, fname)
            relpath = os.path.relpath(fpath, target_dir).replace(os.sep, "/")
            if relpath in exclude_files:
                continue

            try:
                if os.path.getsize(fpath) > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue

            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except OSError:
                continue

            for i, line in enumerate(lines, 1):
                if total >= max_total:
                    break
                for sym in sorted_symbols:
                    if len(refs.get(sym, [])) >= max_per_symbol:
                        continue
                    if compiled[sym].search(line):
                        refs.setdefault(sym, []).append(
                            (relpath, i, line.rstrip()[:120]))
                        total += 1
                        break  # one symbol ref per line

    return refs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_context(target_dir, diff_text):
    """Build a markdown context section for cross-file impact analysis.

    Returns a markdown string for the review prompt, or ``""`` if no
    cross-file references were found. Extracts diff files from *diff_text*
    internally so the caller only needs to pass the raw diff.
    """
    removed, modified = extract_symbols(diff_text)
    all_symbols = sorted(removed | modified)[:MAX_SYMBOLS]
    if not all_symbols:
        return ""

    exclude = _diff_files(diff_text)
    refs = find_references(target_dir, all_symbols, exclude)
    if not refs:
        return ""

    lines = [
        "## Cross-file impact analysis (references to changed symbols in "
        "files NOT in this PR)",
        "",
        "The following symbols were removed, renamed, or had their definitions "
        "modified in this PR. Other files in the repo reference them — check "
        "whether those references will still work after these changes:",
        "",
    ]

    for sym in sorted(refs.keys()):
        status = "removed/renamed" if sym in removed else "modified"
        lines.append(f"### `{sym}` ({status} in this PR)")
        for path, line_num, content in refs[sym]:
            lines.append(f"- `{path}:{line_num}` — `{content.strip()}`")
        lines.append("")

    return "\n".join(lines)
