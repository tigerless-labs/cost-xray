from __future__ import annotations

import pytest

from cost_xray import pricing_map, verify
from cost_xray.adapters import anthropic

MODEL = "claude-opus-4-8"


def _report(monkeypatch, blocks, truth):
    monkeypatch.setattr(pricing_map, "load", lambda: {})
    record = {"request": {"model": MODEL, "messages": []},
              "response": {"streaming": False, "body": {"content": blocks}},
              "usage": {"output_tokens": 100}, "status": 200}
    return verify.bench_turn(record, MODEL, anthropic, per_output_event=truth)


def test_output_event_facet_keeps_unmatched_reference_rows(monkeypatch):
    report = _report(monkeypatch, [{"type": "text", "text": "one"}],
                     [("text", 40), ("thinking", 60)])
    facet = report["facets"]["output_event"]
    missing = facet["residual"][("out", 1, "thinking")]
    assert missing["truth"] == 60
    assert missing["ours"] == 0
    assert missing["abs_rel"] == pytest.approx(1)
    assert verify.aggregate([report])["facets"]["output_event"]["n"] == 2


def test_output_event_facet_keeps_unmatched_observed_rows(monkeypatch):
    report = _report(monkeypatch, [{"type": "text", "text": "one"},
                                  {"type": "text", "text": "two"}], [("text", 100)])
    facet = report["facets"]["output_event"]
    assert facet["ours"][("out", 1, "text")] > 0
    assert facet["residual"][("out", 1, "text")]["truth"] == 0
    assert sum(facet["ours"].values()) == pytest.approx(100)


def test_output_event_facet_keeps_observed_bucket_identity(monkeypatch):
    report = _report(monkeypatch, [{"type": "text", "text": "one"}], [("thinking", 100)])
    facet = report["facets"]["output_event"]
    assert set(facet["ours"]) == {("out", 0, "text")}
    assert set(facet["truth"]) == {("out", 0, "thinking")}
    assert facet["residual"][("out", 0, "thinking")]["abs_rel"] == pytest.approx(1)


def test_output_event_facet_preserves_an_accurate_matching_row(monkeypatch):
    report = _report(monkeypatch, [{"type": "text", "text": "one"}], [("text", 100)])
    facet = report["facets"]["output_event"]
    assert facet["ours"] == facet["truth"] == {("out", 0, "text"): 100}
    assert facet["residual"][("out", 0, "text")]["abs_rel"] == 0
    assert verify.aggregate([report])["facets"]["output_event"]["n"] == 1
