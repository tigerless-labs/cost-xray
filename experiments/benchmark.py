from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import time

from cost_xray import count_tokens as CT
from cost_xray import verify
from cost_xray.adapters import anthropic, openai

SESSIONS = os.path.expanduser("~/.cost-xray/sessions")
REPORTS = pathlib.Path(__file__).resolve().parent / "reports"


def _read_jsonl(path):
    out = []
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return out


def _claude_turns(limit):
    for raw in sorted(glob.glob(f"{SESSIONS}/claude/*/raw.jsonl")):
        for rec in _read_jsonl(raw):
            if isinstance(rec, dict) and isinstance(rec.get("request"), dict) and rec.get("usage"):
                yield rec
                limit[0] -= 1
                if limit[0] <= 0:
                    return


def _codex_turns(limit):
    for raw in sorted(glob.glob(f"{SESSIONS}/codex/*/raw.jsonl")):
        for turn in openai.iter_turns(_read_jsonl(raw)):
            if turn.get("usage"):
                yield turn
                limit[0] -= 1
                if limit[0] <= 0:
                    return


def _bench_codex(turn):
    model = turn.get("model") or "gpt-5-codex"
    return verify.bench_turn(turn, model, openai, thinking_r=openai.THINKING_R)


def _bench_claude(rec):
    req = rec["request"]
    model = rec.get("model") or req.get("model") or ""
    per_tool = CT.per_event_tokens(req, model)
    per_message = CT.per_message_event_tokens(req, model)
    blocks = anthropic.response_blocks(rec)
    per_output_event = CT.per_output_event_tokens(blocks, model) if blocks else None
    output_thinking = CT.output_thinking_tokens(blocks, model) if blocks else None
    return verify.bench_turn(rec, model, anthropic, per_tool=per_tool, per_message=per_message,
                             per_output_event=per_output_event, output_thinking=output_thinking,
                             thinking_r=anthropic.THINKING_R, pin_tools=True)


def _run(label, turns, bench):
    reps, errs = [], 0
    for unit in turns:
        try:
            reps.append(bench(unit))
        except Exception as e:
            errs += 1
            print(f"  [{label}] skipped a turn: {type(e).__name__}: {e}")
    agg = verify.aggregate(reps)
    return {"label": label, "n_turns": len(reps), "errors": errs, "aggregate": agg}


def main():
    ap = argparse.ArgumentParser(description="cost-xray verification benchmark")
    ap.add_argument("--claude", action="store_true",
                    help="include the live Claude axis (count_tokens; needs OAuth/key + network)")
    ap.add_argument("--codex", dest="codex", action="store_true", default=None,
                    help="include the offline Codex axis (default unless --claude-only)")
    ap.add_argument("--claude-only", action="store_true", help="skip the Codex axis")
    ap.add_argument("--limit", type=int, default=30, help="max turns per agent (default 30)")
    args = ap.parse_args()

    runs = []
    if not args.claude_only:
        runs.append(_run("codex", _codex_turns([args.limit]), _bench_codex))
    if args.claude or args.claude_only:
        if CT.auth_headers() is None:
            print("claude axis requested but no count_tokens auth (OAuth login or API key) — "
                  "skipping; completeness-only would still run offline via the codex axis.")
        else:
            runs.append(_run("claude", _claude_turns([args.limit]), _bench_claude))

    print(f"\n# Event-token precision benchmark  (target {args.limit} turns/agent)\n")
    for r in runs:
        n = r["n_turns"]
        flag = "" if n >= args.limit else f"  ⚠ only {n} turns available (< target {args.limit})"
        print(verify.render_markdown(r["aggregate"], title=f"{r['label']} ({n} turns){flag}"))
        print()

    REPORTS.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = REPORTS / f"benchmark-{ts}.json"
    out.write_text(json.dumps({"ts": ts, "runs": runs, "target_turns": args.limit}, indent=2, default=str))
    print(f"structured report → {out}")


if __name__ == "__main__":
    main()
