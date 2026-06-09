"""Materializer — turns captured raw into derived/summary for every stale session.

Runs as a **warm long-lived consumer** the capture proxy starts once and signals per turn (`consume`),
never in the proxy itself (invariant #1) and never gated on the TUI. The consumer **blocks** on the
signal channel, so an idle store costs nothing; it pays the tokenizer import **once** (not per turn);
it coalesces a burst of signals into one sweep; and it exits at EOF when the proxy dies. Discovery is
by disk walk, so a never-opened session is still materialized, and a turn in any session catches up
sessions missed during downtime. See docs/design/read-layer.md, ops.md.

`sweep_once` is one pass (materialize every stale session). `consume` is the warm loop. `main` is a
one-shot CLI for a manual sweep. Per-session locking lives in `materialize_session`, so concurrent
sweeps are safe.
"""
from __future__ import annotations

import argparse
import os
import pathlib

from cost_xray.materialize import materialize_session

ROOT = pathlib.Path(os.path.expanduser("~/.cost-xray/sessions"))


def stale_sessions(root=ROOT):
    """Session dirs whose raw is newer than their summary (or have none) — the ones needing
    (re)materialize. Discovery is by disk walk, so a session that was never opened is found."""
    root = pathlib.Path(root)
    out = []
    for raw in root.glob("*/*/raw.jsonl"):
        d = raw.parent
        try:
            raw_mt = raw.stat().st_mtime
            sp = d / "summary.json"
            sum_mt = sp.stat().st_mtime if sp.exists() else -1.0
        except OSError:
            continue
        if raw_mt > sum_mt:
            out.append(d)
    return out


def sweep_once(root=ROOT):
    """One pass: materialize every stale session. Fail-open per session — one bad session never
    stops the sweep. Returns the dirs processed."""
    done = []
    for d in stale_sessions(root):
        try:
            materialize_session(d)
            done.append(d)
        except Exception:
            pass
    return done


def consume(conn, root=ROOT):
    """Warm loop: block for a turn signal, coalesce any burst, sweep every stale session, repeat.
    Exits when the signal channel reaches EOF — i.e. the proxy that owns the other end has died — so
    the consumer's lifetime is bound to capture. `sweep_once` is fail-open, so a bad session never
    breaks the loop."""
    while True:
        try:
            conn.recv()
        except EOFError:
            return
        try:
            while conn.poll():                # drain pending signals → one sweep per burst
                conn.recv()
        except (EOFError, OSError):
            pass
        sweep_once(root)


def main(argv=None):
    ap = argparse.ArgumentParser(description="cost-xray one-shot materialize sweep")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    sweep_once(pathlib.Path(args.root))


if __name__ == "__main__":
    main()
