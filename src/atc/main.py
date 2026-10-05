"""Main loop. Text mode first (type what you would say); voice plugs into the same `handle()`.

    python -m atc --airport SARC            # fake sim, type your calls
    python -m atc --airport SARC --sim      # real MSFS via SimConnect (add --dll PATH if needed)
    python -m atc --airport SARC --voice m.onnx   # speak replies with Piper (Phase 3)

REPL commands (fake sim only):  /state  /freq 118.1  /wind 270 8 1013  /air  /ground  /quit
"""

from __future__ import annotations

import argparse
from pathlib import Path

from atc.airports.schema import load_airport
from atc.facility import resolve_facility
from atc.llm.client import make_llm
from atc.llm.prompt import build_context, build_messages, build_system_prompt
from atc.models import Airport
from atc.sim.base import SimSource

TRAFFIC_RADIUS_NM = 15.0


def handle(airport: Airport, sim: SimSource, llm, speaker, history: list, pilot_text: str) -> str | None:
    """One pilot transmission -> one ATC reply (or None if nobody answers)."""
    own = sim.own()
    facility = resolve_facility(airport, own.com1_mhz)
    if facility is None:
        print(f"[no station on {own.com1_mhz:.3f}]")
        return None
    if not facility.can_reply:
        print(f"[{facility.role} on {own.com1_mhz:.3f}: nobody answers here]")
        return None

    traffic = sim.traffic(airport.lat, airport.lon, TRAFFIC_RADIUS_NM)
    messages = build_messages(
        build_system_prompt(airport, facility),
        build_context(own, traffic, airport),
        history,
        pilot_text,
    )
    try:
        reply = llm.complete(messages)
    except Exception as exc:  # network, rate limit, bad model: never crash the session
        print(f"[LLM error, say again: {exc}]")
        return None
    history.append((pilot_text, reply))
    speaker.say(reply)
    return reply


def _repl_command(cmd: str, sim) -> bool:
    """Returns False to quit."""
    parts = cmd.split()
    name = parts[0]
    if name == "/quit":
        return False
    if name == "/state":
        print(sim.own())
    elif name == "/freq" and len(parts) == 2 and hasattr(sim, "update"):
        sim.update(com1_mhz=float(parts[1]))
    elif name == "/wind" and len(parts) in (3, 4) and hasattr(sim, "update"):
        changes = {"wind_dir_deg": float(parts[1]), "wind_kt": float(parts[2])}
        if len(parts) == 4:
            changes["qnh_hpa"] = float(parts[3])
        sim.update(**changes)
    elif name == "/air" and hasattr(sim, "set_airborne"):
        sim.set_airborne()
    elif name == "/ground" and hasattr(sim, "set_on_ground"):
        sim.set_on_ground()
    else:
        print("commands: /state /freq <mhz> /wind <dir> <kt> [qnh_hpa] /air /ground /quit (fake sim only)")
    return True


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="atc")
    p.add_argument("--airport", required=True, help="ICAO code; file is airports/<ICAO>.yaml")
    p.add_argument("--airports-dir", type=Path, default=Path("airports"))
    p.add_argument("--sim", action="store_true", help="use real MSFS (default: fake sim)")
    p.add_argument("--voice", type=Path, help="Piper .onnx voice file; omit for text only")
    p.add_argument("--callsign", default="N123AB")
    p.add_argument("--dll", help="path to SimConnect.dll (MSFS 2024 SDK) if the bundled one fails")
    args = p.parse_args(argv)

    airport = load_airport(args.airports_dir / f"{args.airport.upper()}.yaml")
    if airport.needs_review:
        print(f"note: {airport.icao}.yaml has needs_review: true (pattern rules may be missing)")

    if args.sim:
        from atc.sim.simconnect_source import SimConnectSource

        sim: SimSource = SimConnectSource(callsign=args.callsign, library_path=args.dll)
    else:
        from atc.sim.fake import FakeSim

        sim = FakeSim(airport, callsign=args.callsign)

    if args.voice:
        from atc.audio.tts import PiperTTS

        speaker = PiperTTS(args.voice)
    else:
        from atc.audio.tts import PrintTTS

        speaker = PrintTTS()

    llm = make_llm()
    history: list[tuple[str, str]] = []
    print(f"{airport.icao} {airport.name}. Type your radio calls. /quit to exit.")
    try:
        while True:
            try:
                line = input("YOU> ").strip()
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                if not _repl_command(line, sim):
                    break
                continue
            handle(airport, sim, llm, speaker, history, line)
    finally:
        sim.close()


if __name__ == "__main__":
    main()
