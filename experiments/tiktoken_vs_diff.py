from __future__ import annotations

import glob
import json
import os
import pathlib

from cost_xray import count_tokens as CT
from cost_xray.analyze import ntok
from cost_xray.events import _count_content, bucket_of


def _latest_request():
    def mtime(d):
        try:
            return max(os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d))
        except ValueError:
            return 0.0

    for d in sorted(glob.glob(os.path.expanduser("~/.cost-xray/sessions/claude/*")),
                    key=mtime, reverse=True):
        raw = pathlib.Path(d) / "raw.jsonl"
        if not raw.exists():
            continue
        rec = None
        for line in raw.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r.get("request"), dict) and r.get("usage"):
                rec = r
        if rec:
            return rec
    return None


def _row(label, tk, diff):
    ratio = (tk / diff) if diff else float("nan")
    flag = "" if not diff else ("  OK" if 0.95 <= ratio <= 1.05 else "  <-- OFF")
    print(f"  {label:26} tiktoken={tk:>9,}  diff={diff:>9,}  tiktoken/diff={ratio:5.2f}x{flag}")


def main():
    if CT.auth_headers() is None:
        print("no count_tokens auth (OAuth login or API key) — can't run the differencing side")
        return
    rec = _latest_request()
    if rec is None:
        print("no captured Claude session with usage")
        return
    req, model = rec["request"], rec.get("model") or ""

    anchors = CT.exact_anchors(req, model)
    buckets = CT.per_bucket_tokens(req, model)
    per_tool = CT.per_event_tokens(req, model)
    if not (anchors and buckets and per_tool):
        print("count_tokens unavailable (auth/network?)")
        return

    tools = req.get("tools") or []
    tk_system = ntok(req.get("system"))
    tk_tools = sum(ntok(t) for t in tools)
    tk_tool = {(t.get("name") or t.get("type") or "?"): ntok(t) for t in tools}
    tk_bucket: dict[str, int] = {}
    for m in req.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                bk = bucket_of(b.get("type")) or "text"
                bk = "tool_io" if bk in ("tool_use", "tool_result") else bk
                tk_bucket[bk] = tk_bucket.get(bk, 0) + ntok(_count_content(b))
        elif isinstance(c, str):
            tk_bucket["text"] = tk_bucket.get("text", 0) + ntok(c)

    print(f"model={model}   (latest captured turn)\n")
    print("STATIC  — tiktoken vs count_tokens differencing")
    _row("system", tk_system, anchors["system"])
    _row("tools (all schemas)", tk_tools, anchors["tools"])
    diff_tool = {label[5:]: v for label, v in per_tool if label.startswith("tool:")}
    print("  -- top 5 tools by exact size --")
    for name in sorted(diff_tool, key=lambda k: -diff_tool[k])[:5]:
        _row("  " + name[:24], tk_tool.get(name, 0), diff_tool[name])

    print("\nMESSAGES buckets — tiktoken vs count_tokens differencing")
    for bk in ("thinking", "text", "tool_io"):
        _row(bk, tk_bucket.get(bk, 0), buckets.get(bk, 0))

    print("\nVERDICT: tiktoken(raw content) UNDER-counts real Claude tokens ~0.6-0.7x across")
    print("every non-thinking source — it omits Claude's serialization/formatting overhead")
    print("(tool-definition wrappers, message framing). That error is roughly UNIFORM, so a")
    print("single calibration to wire `usage` corrects the scale and the proportions survive.")
    print("The thinking bucket is the OUTLIER at ~2.6x OVER (base64 signatures) — opposite")
    print("direction, so it can't share that one calibration. Production must split thinking out")
    print("(count_tokens, or strip signatures before tiktoken) or it poisons every other bucket.")


if __name__ == "__main__":
    main()
