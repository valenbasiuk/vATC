"""STEP 1 on the PC: print your own aircraft's data from MSFS, once per second.

    python tools/probe_own.py              # 15 seconds
    python tools/probe_own.py --seconds 60 --dll "C:\\path\\to\\SimConnect.dll"

Start MSFS, load a flight (cockpit visible), then run this. Compare every printed value with what
the cockpit/sim shows. Things to confirm are listed in docs/PRE_IDE_CHECKLIST.md.
"""

from __future__ import annotations

import argparse
import time

from atc.sim.simconnect_source import SimConnectSource


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--seconds", type=int, default=15)
    p.add_argument("--dll", help="path to SimConnect.dll (MSFS 2024 SDK) if the bundled one fails")
    args = p.parse_args()

    src = SimConnectSource(library_path=args.dll)
    try:
        for _ in range(args.seconds):
            o = src.own()
            wind = "unknown" if o.wind_kt is None else f"{o.wind_dir_deg:.0f}@{o.wind_kt:.0f}kt"
            qnh = "unknown" if o.qnh_hpa is None else f"{o.qnh_hpa:.1f}hPa"
            print(
                f"lat {o.lat:.5f} lon {o.lon:.5f} | MSL {o.alt_msl_ft:.0f} AGL {o.alt_agl_ft:.0f} ft | "
                f"GS {o.gs_kt:.0f} kt | HDG {o.heading_deg:.0f} | ground={o.on_ground} | "
                f"COM1 {o.com1_mhz} | squawk {o.squawk} | wind {wind} | QNH {qnh}"
            )
            time.sleep(1)
    finally:
        src.close()


if __name__ == "__main__":
    main()
