from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import select
import sys

from cost_xray.materialize import materialize_session, set_rollup_broken

ROOT = pathlib.Path(os.path.expanduser("~/.cost-xray/sessions"))
_LOG = logging.getLogger("cost_xray.materialize_daemon")
_BROKEN_STATE: dict = {}


def stale_sessions(root=ROOT):
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


def capture_broken_sessions(root=ROOT):
    root = pathlib.Path(root)
    out = []
    for meta in root.glob("*/*/meta.json"):
        d = meta.parent
        if (d / "raw.jsonl").exists():
            continue
        try:
            n_turns = json.loads(meta.read_text()).get("n_turns", 0)
        except Exception:
            continue
        if n_turns > 0:
            out.append(d)
    return out


def _flag_broken(root):
    root = pathlib.Path(root)
    by_agent = {ad: [] for ad in root.iterdir() if ad.is_dir()}
    for d in capture_broken_sessions(root):
        by_agent.setdefault(d.parent, []).append(d.name)
    for agent_dir, names in by_agent.items():
        names = sorted(names)
        if _BROKEN_STATE.get(str(agent_dir)) == names:
            continue
        for sid in set(names) - set(_BROKEN_STATE.get(str(agent_dir)) or []):
            _LOG.warning("capture-broken session (meta advances, no raw): %s", agent_dir / sid)
        set_rollup_broken(agent_dir, names)
        _BROKEN_STATE[str(agent_dir)] = names


def sweep_once(root=ROOT):
    done = []
    for d in stale_sessions(root):
        try:
            materialize_session(d)
            done.append(d)
        except Exception:
            pass
    try:
        _flag_broken(root)
    except Exception as e:
        _LOG.warning("capture-health flagging failed: %r", e)
    return done


def watch(reader=None, root=ROOT):
    f = reader if reader is not None else sys.stdin.buffer
    fd = f.fileno()
    while True:
        if not os.read(fd, 4096):
            return
        while select.select([fd], [], [], 0)[0]:
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
