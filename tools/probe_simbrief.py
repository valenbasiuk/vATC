"""Fetch your latest SimBrief OFP and show what is in it. No API key needed (only generating plans needs one).

    python tools/probe_simbrief.py --username YOUR_NAVIGRAPH_ALIAS
    python tools/probe_simbrief.py --userid 123456            # your SimBrief Pilot ID
    (or set ATC_SIMBRIEF_USER / ATC_SIMBRIEF_USERID)

Generate a plan on simbrief.com first (any flight; a VFR-ish one with a short route is fine).
Saves the full JSON to simbrief_last.json (git-ignored) and prints the key tree plus the fields
this project cares about. Bring the output to the IDE session; the loader gets written from it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = "https://www.simbrief.com/api/xml.fetcher.php"


def fetch(param: str, value: str) -> dict:
    url = f"{URL}?{urllib.parse.urlencode({param: value, 'json': 'v2'})}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        sys.exit(f"SimBrief answered HTTP {exc.code} (wrong username/pilot id, or no plan yet?): {body}")


def tree(node, prefix: str = "", depth: int = 0, max_depth: int = 2) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                size = f"[{len(v)}]" if isinstance(v, list) else ""
                print(f"{prefix}{k}{size}")
                if depth < max_depth:
                    tree(v[0] if isinstance(v, list) and v else v, prefix + "  ", depth + 1, max_depth)
            else:
                s = str(v)
                print(f"{prefix}{k} = {s[:60]}{'...' if len(s) > 60 else ''}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--username", default=os.environ.get("ATC_SIMBRIEF_USER"))
    p.add_argument("--userid", default=os.environ.get("ATC_SIMBRIEF_USERID"))
    args = p.parse_args()
    if args.userid:
        data = fetch("userid", args.userid)
    elif args.username:
        data = fetch("username", args.username)
    else:
        sys.exit("give --username or --userid")

    with open("simbrief_last.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    print("saved simbrief_last.json\n\n=== key tree ===")
    tree(data)

    print("\n=== fields this project wants (checked against a real OFP, 2026-10) ===")
    g = lambda *path: _dig(data, path)  # noqa: E731
    for label, path in [
        ("callsign", ("atc", "callsign")),
        ("flight rules", ("atc", "flight_rules")),  # I / V / Y / Z; atc.flight_type is S=scheduled etc.
        ("aircraft", ("aircraft", "icaocode")),
        ("origin", ("origin", "icao_code")),
        ("origin runway", ("origin", "plan_rwy")),
        ("destination", ("destination", "icao_code")),
        ("destination runway", ("destination", "plan_rwy")),
        ("alternate", ("alternate", 0, "icao_code")),  # a list, one entry per alternate
        ("route", ("general", "route")),
        ("initial altitude", ("general", "initial_altitude")),  # no separate cruise field; see stepclimb_string
        ("step climbs", ("general", "stepclimb_string")),
        ("SID", ("general", "sid_ident")),
        ("STAR", ("general", "star_ident")),
        ("ATC route", ("atc", "route")),
    ]:
        print(f"{label:20s} {g(*path)}")
    fixes = data.get("navlog") or []
    if isinstance(fixes, dict):  # older json shape: {"fix": [...]}
        fixes = fixes.get("fix") or []
    print(f"{'navlog fixes':20s} {len(fixes)}: " + " ".join(str(x.get("ident")) for x in fixes[:25] if isinstance(x, dict)))


def _dig(d, path):
    for k in path:
        if isinstance(k, int) and isinstance(d, list) and k < len(d):
            d = d[k]
        elif isinstance(d, dict) and k in d:
            d = d[k]
        else:
            return "(not found)"
    return d


if __name__ == "__main__":
    main()
