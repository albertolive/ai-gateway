"""Tests for scripts/usage.py — the per-cascade usage aggregator."""

import json

import usage


SAMPLE = """\
2026-08-19T11:22:33.000Z {"event": "usage", "cascade": "general", "model": "gemini/gemini-3.6-flash", "status": "ok", "elapsed_ms": 400}
{"event": "usage", "cascade": "general", "model": "gemini/gemini-3.6-flash", "status": "ok", "elapsed_ms": 500}
{"event": "usage", "cascade": "frontier", "model": null, "status": "error", "elapsed_ms": 1200}
some unrelated noise line
{"event": "usage", "cascade": "creative", "model": "openrouter/google/gemma-4-26b-a4b-it:free", "status": "ok", "elapsed_ms": 300}
"""

# A real `vercel logs --json` line: the usage event is escaped inside `message`.
VERCEL_JSON = json.dumps({
    "id": "abc",
    "timestamp": 1787165746478,
    "level": "info",
    "message": json.dumps({
        "event": "usage", "cascade": "creative",
        "model": "gemini/gemini-3.5-flash-lite", "status": "ok",
        "elapsed_ms": 623, "created": 1787165747,
    }),
    "source": "serverless",
    "responseStatusCode": 200,
})


def test_parse_events_ignores_noise():
    events = list(usage.parse_events(SAMPLE))
    assert len(events) == 4


def test_parse_events_handles_vercel_json_format():
    events = list(usage.parse_events(VERCEL_JSON + "\n"))
    assert len(events) == 1
    assert events[0]["cascade"] == "creative"
    assert events[0]["model"] == "gemini/gemini-3.5-flash-lite"
    assert events[0]["status"] == "ok"


def test_parse_events_strips_ansi():
    ansi = "\x1b[31m" + SAMPLE.splitlines()[0] + "\x1b[0m"
    events = list(usage.parse_events(ansi))
    assert len(events) == 1
    assert events[0]["cascade"] == "general"


def test_aggregate_counts_per_cascade():
    rows, models = usage.aggregate(SAMPLE)
    by_cascade = {r["cascade"]: r for r in rows}

    assert by_cascade["general"]["calls"] == 2
    assert by_cascade["general"]["ok"] == 2
    assert by_cascade["general"]["error"] == 0
    assert by_cascade["general"]["avg_ms"] == 450

    assert by_cascade["frontier"]["calls"] == 1
    assert by_cascade["frontier"]["error"] == 1

    assert by_cascade["creative"]["calls"] == 1
    assert models["general"]["gemini/gemini-3.6-flash"] == 2
    assert models["frontier"][None] == 1


def test_aggregate_empty_input():
    rows, models = usage.aggregate("no events here")
    assert rows == []
    assert models == {}
