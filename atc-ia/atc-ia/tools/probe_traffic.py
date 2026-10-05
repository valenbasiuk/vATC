"""STEP 2 on the PC: list AI aircraft around you, straight from SimConnect.

    python tools/probe_traffic.py                    # 40 km radius, 5 reads
    python tools/probe_traffic.py --radius-km 100 --reads 10 --dll "C:\\path\\to\\SimConnect.dll"

Best test: spawn at a busy default airport with AI traffic enabled in the sim settings (traffic
slider up). Expected: callsigns, positions, altitudes, on-ground flags that match what you see
outside. If it prints nothing or errors, copy the whole output into the IDE session: this is the
single biggest unknown in the project (docs/OPEN_QUESTIONS.md #1).

Also check: is YOUR OWN aircraft in the list? Does the list include multiplayer/online traffic
(MSFS sometimes returns those too, with huge object ids)?
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from atc.geo import distance_nm
from atc.sim.ai_traffic import AiTrafficReader
from atc.sim.simconnect_source import SimConnectSource


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--radius-km", type=float, default=40.0)
    p.add_argument("--reads", type=int, default=5)
    p.add_argument("--dll", help="path to SimConnect.dll (MSFS 2024 SDK)")
    args = p.parse_args()

    own_src = SimConnectSource(library_path=args.dll)
    reader = AiTrafficReader(Path(args.dll) if args.dll else None)
    try:
        for i in range(args.reads):
            own = own_src.own()
            recs = reader.read(int(args.radius_km * 1000))
            print(f"--- read {i + 1}: {len(recs)} aircraft within {args.radius_km:.0f} km | you: {own.lat:.4f},{own.lon:.4f}")
            for r in sorted(recs, key=lambda r: distance_nm(own.lat, own.lon, r.lat, r.lon)):
                d = distance_nm(own.lat, own.lon, r.lat, r.lon)
                me = "  <-- looks like YOU" if d < 0.01 else ""
                print(
                    f"  id {r.object_id:>10} {r.atc_id or '(no callsign)':<10} {d:6.1f} NM | "
                    f"{r.alt_ft:7.0f} ft | {r.gs_kt:4.0f} kt | hdg {r.heading_deg:3.0f} | "
                    f"{'ground' if r.on_ground else 'air'}{me}"
                )
            time.sleep(2)
    finally:
        reader.close()
        own_src.close()


if __name__ == "__main__":
    main()
