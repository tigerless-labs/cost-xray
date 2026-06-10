from __future__ import annotations

import json
import urllib.request

from cost_xray import pricing_map


def main() -> None:
    with urllib.request.urlopen(pricing_map._URL, timeout=30) as resp:
        m = json.loads(resp.read().decode())
    if not pricing_map._valid(m):
        raise SystemExit("fetched price map failed validation")
    pricing_map._BUNDLED.write_text(json.dumps(m))
    print(f"wrote {len(m)} models to {pricing_map._BUNDLED}")


if __name__ == "__main__":
    main()
