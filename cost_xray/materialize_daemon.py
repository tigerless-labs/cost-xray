"""Materializer — turns captured raw into derived/summary for every stale session.

Runs as a **warm long-lived consumer** the capture proxy starts once and signals per turn (`watch`),
never in the proxy itself (invariant #1) and never gated on the TUI. The consumer **blocks** on the
signal channel, so an idle store costs nothing; it pays the tokenizer import **once** (not per turn);
it coalesces a burst of signals into one sweep; and it exits at EOF when the proxy dies. Discovery is
by disk walk, so a never-opened session is still materialized, and a turn in any session catches up
sessions missed during downtime. See docs/design/read-layer.md, ops.md.

`sweep_once` is one pass (materialize every stale session). `watch` is the warm loop (wake bytes on
stdin). `main` is the CLI: a one-shot sweep, or `--watch` to run the warm loop. Per-session locking
lives in `materialize_session`, so concurrent sweeps are safe.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import select
import sys

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


def watch(reader=None, root=ROOT):
    """Warm loop: block on the wake channel (the proxy's pipe to our stdin) for one byte, coalesce
    any bytes already waiting into a single sweep of every stale session, repeat. Returns at EOF —
    the proxy that owns the write end has closed it — so the watcher's lifetime is bound to capture.
    `sweep_once` is fail-open, so a bad session never breaks the loop."""
    f = reader if reader is not None else sys.stdin.buffer
    fd = f.fileno()
    while True:
        if not os.read(fd, 4096):             # blocks until ≥1 byte or EOF (b"")
            return
        while select.select([fd], [], [], 0)[0]:   # drain a burst → one sweep
            if not os.read(fd, 4096):
                sweep_once(root)
                return
        sweep_once(root)


def main(argv=None):
    ap = argparse.ArgumentParser(description="cost-xray materialize: one-shot sweep, or --watch warm loop")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--watch", action="store_true",
                    help="warm loop: sweep on each wake byte read from stdin; exit at EOF")
    args = ap.parse_args(argv)
    if args.watch:
        watch(root=pathlib.Path(args.root))
    else:
        sweep_once(pathlib.Path(args.root))


if __name__ == "__main__":
    main()
