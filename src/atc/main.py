"""Main loop. Text mode first (type what you would say); voice plugs into the same `handle()`.

    python -m atc --airport SARC            # fake sim, type your calls
    python -m atc --airport SARC --sim      # real MSFS via SimConnect (add --dll PATH if needed)
    python -m atc --airport SARC --voice m.onnx   # speak replies with Piper (Phase 3)

REPL commands (fake sim only):  /state  /freq 118.1  /wind 270 8 1013  /air  /ground  /quit
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from atc.airports.schema import load_airport
from atc.facility import resolve_facility
from atc.clearance import context_lines, handle_clearance, handle_push
from atc.flightplan import load_simbrief
from atc.llm.client import make_llm
from atc.llm.prompt import build_context, build_messages, build_system_prompt
from atc.models import Airport
from atc.readback import check_readback, is_acknowledgement
from atc.session import Session
from atc.sim.base import SimSource

TRAFFIC_RADIUS_NM = 15.0


def handle(
    airport: Airport,
    sim: SimSource,
    llm,
    speaker,
    history: list,
    pilot_text: str,
    stt_s: float | None = None,
    session: Session | None = None,
) -> str | None:
    """One pilot transmission -> one ATC reply (or None if nobody answers)."""
    own = sim.own()
    facility = resolve_facility(airport, own.com1_mhz)
    if facility is None:
        print(f"[no station on {own.com1_mhz:.3f}]")
        return None
    if not facility.can_reply:
        print(f"[{facility.role} on {own.com1_mhz:.3f}: nobody answers here]")
        return None
    if session is None:
        session = Session(callsign=own.callsign)
    session.learn_telephony(pilot_text)
    cs = session.spoken_callsign

    reply = None
    if session.plan is not None:  # IFR clearance is code's job: issue, readback check, correction
        reply = handle_clearance(session, airport, facility, pilot_text,
                                 session.dest_name or session.plan.destination_name)
    if reply is None:
        reply = handle_push(session, airport, facility, pilot_text)
    last_atc = history[-1][1] if history else None
    rb = check_readback(last_atc, pilot_text)
    if reply is None and (
        rb.status == "correct"
        or (rb.status == "none" and is_acknowledgement(last_atc, pilot_text, tuple(cs.split())))
    ):
        # Decided by code, no LLM call. Real controllers don't answer a correct readback or a "roger".
        print("[ATC: no reply needed]")
        return None
    if reply is None and rb.status == "incomplete" and last_atc:
        # Wrong readback: say the instruction again, word for word (ICAO "negative, I say again").
        instruction = last_atc.removeprefix(cs).lstrip(" ,")
        reply = f"{cs}, negative, I say again, {instruction}"
    if reply is not None:
        history.append((pilot_text, reply))
        _say_timed(speaker, reply, stt_s, 0.0)
        return reply

    traffic = sim.traffic(airport.lat, airport.lon, TRAFFIC_RADIUS_NM)
    context = build_context(own, traffic, airport, cs, facility.role, session.first_contact(facility.role))
    plan_lines = context_lines(session, airport, facility)
    if plan_lines:
        context += "\n" + "\n".join(plan_lines)
    messages = build_messages(
        build_system_prompt(airport, facility),
        context,
        history,
        pilot_text,
    )
    t_llm = time.perf_counter()
    try:
        reply = llm.complete(messages)
    except Exception as exc:  # network, rate limit, bad model: never crash the session
        print(f"[LLM error, say again: {exc}]")
        return None
    llm_s = time.perf_counter() - t_llm
    history.append((pilot_text, reply))
    _say_timed(speaker, reply, stt_s, llm_s)
    return reply


def _say_timed(speaker, reply: str, stt_s: float | None, llm_s: float) -> None:
    """Speak, then print per-stage latency (roadmap item 15). Target: under 3-4 s to first audio."""
    speaker.say(reply)
    synth_s = getattr(speaker, "last_synth_s", None)  # PiperTTS only; PrintTTS has no audio
    if stt_s is None and synth_s is None:
        return
    parts = [f"stt {stt_s:.1f}s"] if stt_s is not None else []
    parts.append(f"llm {llm_s:.1f}s")
    if synth_s is not None:
        parts.append(f"tts {synth_s:.1f}s")
    first_audio = (stt_s or 0.0) + llm_s + (synth_s or 0.0)
    print(f"[timing] {' + '.join(parts)} = {first_audio:.1f}s to first audio")


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
    p.add_argument("--ptt", action="store_true", help="push-to-talk input (hold the key, speak, release)")
    p.add_argument("--ptt-key", default="f9")
    p.add_argument("--stt-model", default="small.en", help="faster-whisper model (try base.en if too slow)")
    p.add_argument("--callsign", default="N123AB")
    p.add_argument("--telephony", help='airline radio name, e.g. "Martinair" (else learned from your first call)')
    p.add_argument("--dll", help="path to SimConnect.dll (MSFS 2024 SDK) if the bundled one fails")
    p.add_argument("--simbrief", type=Path, help="SimBrief OFP json (tools/probe_simbrief.py saves simbrief_last.json)")
    args = p.parse_args(argv)

    plan = load_simbrief(args.simbrief) if args.simbrief else None
    if plan is not None:
        args.callsign = plan.callsign or args.callsign
        print(f"flight plan: {plan.callsign} {plan.origin}-{plan.destination} {plan.sid or ''} squawk {plan.squawk}")

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

    session = Session(callsign=args.callsign, plan=plan, telephony=args.telephony)
    if plan is not None:
        dest_file = args.airports_dir / f"{plan.destination}.yaml"
        if dest_file.exists():  # its spoken_name is how the clearance limit is said ("Rosario")
            dest = load_airport(dest_file)
            session.dest_name = dest.spoken_name or dest.name

    llm = make_llm()
    history: list[tuple[str, str]] = []
    try:
        if args.ptt:
            _run_ptt(args, airport, sim, llm, speaker, history, session)
        else:
            _run_text(airport, sim, llm, speaker, history, session)
    except KeyboardInterrupt:
        pass
    finally:
        sim.close()


def _run_text(airport, sim, llm, speaker, history, session) -> None:
    print(f"{airport.icao} {airport.name}. Type your radio calls. /quit to exit.")
    while True:
        try:
            line = input("YOU> ").lstrip("﻿").strip()  # piped input from PowerShell starts with a BOM
        except EOFError:
            break
        if not line:
            continue
        if line.startswith("/"):
            if not _repl_command(line, sim):
                break
            continue
        handle(airport, sim, llm, speaker, history, line, session=session)


def _run_ptt(args, airport, sim, llm, speaker, history, session) -> None:
    from atc.audio.ptt import PushToTalk
    from atc.audio.stt import FasterWhisperSTT

    print(f"loading speech model {args.stt_model} ...")
    stt = FasterWhisperSTT(model_size=args.stt_model)
    ptt = PushToTalk(key=args.ptt_key)
    print(f"{airport.icao} {airport.name}. Hold {args.ptt_key.upper()} to talk. Ctrl+C to exit.")
    while True:
        audio = ptt.record_once()
        if len(audio) < 4800:  # under 0.3 s: a tap, not a call
            continue
        t0 = time.perf_counter()
        text = stt.transcribe(audio)
        stt_s = time.perf_counter() - t0
        if not text:
            print("[nothing heard]")
            continue
        print(f"YOU> {text}")
        handle(airport, sim, llm, speaker, history, text, stt_s=stt_s, session=session)


if __name__ == "__main__":
    main()
