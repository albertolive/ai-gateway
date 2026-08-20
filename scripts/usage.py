#!/usr/bin/env python3
"""Aggregate per-cascade usage from the hosted gateway's log lines.

The gateway (api/chat/completions.py) emits one flat JSON line per request:
    {"event": "usage", "cascade": ..., "model": ..., "status": ..., ...}
This script scans those lines out of `vercel logs` output and summarizes calls
per cascade and per model. It understands both `vercel logs --json` (where the
event sits inside a `message` field) and plain-text log dumps.

Usage:
    vercel logs <app> --no-follow --json | python3 scripts/usage.py
    python3 scripts/usage.py < gateway.log

Stdlib only; no dependencies.
"""

import json
import re
import sys
from collections import Counter, defaultdict

# Flat usage JSON has no nested braces, so this regex grabs the whole object.
_USAGE_RE = re.compile(r'\{"event": "usage"[^}]*\}')
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def parse_events(text):
    """Yield usage-event dicts found in `text` (Vercel --json or plain)."""
    text = _ANSI_RE.sub("", text)  # strip `vercel logs` color codes

    for line in text.splitlines():
        # Vercel `--json` wraps the log line: {..., "message": "{usage json}", ...}
        try:
            outer = json.loads(line)
        except json.JSONDecodeError:
            outer = None
        if isinstance(outer, dict):
            # Case A: the line itself is a usage event.
            if outer.get("event") == "usage":
                yield outer
                continue
            # Case B: Vercel --json wraps the event in a `message` field.
            try:
                inner = json.loads(outer.get("message", ""))
            except (json.JSONDecodeError, TypeError):
                inner = None
            if isinstance(inner, dict) and inner.get("event") == "usage":
                yield inner
            continue

        # Plain-text fallback: a flat usage JSON anywhere on the line.
        for match in _USAGE_RE.finditer(line):
            try:
                event = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if event.get("event") == "usage":
                yield event


def aggregate(text):
    """Return (rows, models) summaries.

    `rows` is a list of dicts (cascade, calls, ok, error, avg_ms), sorted by
    cascade name. `models` maps cascade -> Counter of model -> calls.
    """
    calls = Counter()
    ok = Counter()
    err = Counter()
    elapsed_ms = defaultdict(int)
    models = defaultdict(Counter)

    for ev in parse_events(text):
        cascade = ev.get("cascade", "?")
        status = ev.get("status", "?")
        calls[cascade] += 1
        if status == "ok":
            ok[cascade] += 1
        else:
            err[cascade] += 1
        elapsed_ms[cascade] += ev.get("elapsed_ms", 0)
        models[cascade][ev.get("model", "?")] += 1

    rows = [
        {
            "cascade": cascade,
            "calls": calls[cascade],
            "ok": ok[cascade],
            "error": err[cascade],
            "avg_ms": elapsed_ms[cascade] // calls[cascade],
        }
        for cascade in sorted(calls)
    ]
    return rows, models


def main():
    text = sys.stdin.read()
    rows, models = aggregate(text)
    if not rows:
        print("No usage events found.")
        print("Run: vercel logs <app> --no-follow --json | python3 scripts/usage.py")
        return

    print(f"{'cascade':<14} {'calls':>6} {'ok':>4} {'err':>4} {'avg_ms':>8}")
    for r in rows:
        print(f"{r['cascade']:<14} {r['calls']:>6} {r['ok']:>4} "
              f"{r['error']:>4} {r['avg_ms']:>8}")

    print("\nmodels per cascade:")
    for cascade in sorted(models):
        for model, count in models[cascade].most_common():
            print(f"  {cascade:<12} {model}: {count}")


if __name__ == "__main__":
    main()
