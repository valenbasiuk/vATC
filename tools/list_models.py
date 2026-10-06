"""List the models each provider offers right now (free tiers change often; model names go 404/410).

    python tools/list_models.py            # every provider with a key
    python tools/list_models.py groq
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atc.llm.client import PROVIDERS, env_key  # noqa: E402


def main(argv: list[str]) -> int:
    for name, (url, var) in PROVIDERS.items():
        if argv and name not in argv:
            continue
        key = env_key(var)
        if not key:
            print(f"{name}: no {var}")
            continue
        req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}",
                                                               "User-Agent": "atc-ia/0.1 (personal flight-sim ATC)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: {exc}")
            continue
        ids = sorted(m.get("id", "") for m in data.get("data", []))
        print(f"{name} ({len(ids)}): " + ", ".join(i.removeprefix("models/") for i in ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
