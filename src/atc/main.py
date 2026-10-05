"""Main loop. Text mode first (type what you would say); voice plugs into the same `handle()`.

    python -m atc --airport SARC            # fake sim, type your calls
    python -m atc --airport SARC --sim      # real MSFS via SimConnect (add --dll PATH if needed)
    python -m atc --airport SARC --voice m.onnx   # speak replies with Piper (Phase 3)

REPL commands (fake sim only):  /state  /freq 118.1  /wind 270 8 1013  /air  /ground
                                /final 3 [31] (AI arrival on 3 NM final)  /onrwy [31]  /notraffic  /quit
"""

from __future__ import annotations

import argparse
import random
import threading
import time
from dataclasses import replace
from pathlib import Path

from atc import phrase
from atc.airports.schema import load_airport
from atc.facility import callsign_for, resolve_facility
from atc.clearance import context_lines, deliver_after_standby, handle_clearance, handle_push
from atc.flightplan import load_simbrief
from atc.geo import distance_nm
from atc.llm.client import make_llm
from atc.llm.prompt import build_context, build_messages, build_system_prompt
from atc.models import Airport
from atc.readback import check_readback, is_acknowledgement
from atc.runway import runway_in_use
from atc.sequence import context_lines as sequence_context
from atc.sequence import guard, runway_status
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
    own = _surface_wind(own, airport, session)
    session.learn_telephony(pilot_text)
    cs = session.spoken_callsign
    station = callsign_for(airport, facility)

    reply = None
    if session.names_other_flight(pilot_text):  # misheard or wrong callsign: never answer it as ours
        reply = f"Station calling {station}, say again your callsign."
    if reply is None and session.plan is not None:  # IFR clearance is code's job: issue, readback check, correction
        reply = handle_clearance(session, airport, facility, pilot_text,
                                 session.dest_name or session.plan.destination_name)
    if reply is None:
        reply = handle_push(session, airport, facility, pilot_text)
    # A readback only answers the position that gave the instruction: after "contact Tower", the first call
    # on Tower is a new call even if it repeats Ground's "holding point runway 31".
    last_atc = history[-1][1] if history and session.last_role in (None, facility.role) else None
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
        instruction = last_atc.removeprefix(cs).lstrip(" ,").removeprefix(station).lstrip(" ,")
        reply = f"{cs}, negative, I say again, {instruction}"
    if reply is not None:
        history.append((pilot_text, reply))
        session.last_role = facility.role
        _say_timed(speaker, reply, stt_s, 0.0)
        return reply

    plan = session.plan
    preferred = plan.planned_runway if plan and plan.origin == airport.icao and own.on_ground else None
    traffic = sim.traffic(airport.lat, airport.lon, TRAFFIC_RADIUS_NM)
    context = build_context(own, traffic, airport, cs, facility.role, session.first_contact(facility.role),
                            preferred_runway=preferred)
    plan_lines = context_lines(session, airport, facility)
    if plan and plan.is_ifr and session.clearance == "confirmed" and own.squawk != plan.squawk \
            and facility.role in ("tower", "departure", "approach"):
        plan_lines.append(f"  TRANSPONDER WRONG: the pilot squawks {own.squawk}, assigned {plan.squawk}. "
                          f"Start your reply with '{cs}, squawk {phrase.digits(plan.squawk)}'.")
    status = None
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt, preferred)
    if facility.role == "tower" and rwy is not None:  # who may take off / land is code's decision
        status = runway_status(airport, rwy, own, traffic)
        plan_lines += sequence_context(status, own)
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
        print(f"[LLM error, nobody answers: {str(exc)[:200]}]")
        return None
    llm_s = time.perf_counter() - t_llm
    safe = guard(reply, status, cs, own)
    if safe != reply:
        print(f"[sequence guard replaced: {reply}]")
        reply = safe
    history.append((pilot_text, reply))
    session.last_role = facility.role
    _say_timed(speaker, reply, stt_s, llm_s)
    return reply


SURFACE_WIND_AGL_FT = 3000.0
SURFACE_WIND_RADIUS_NM = 10.0


def _surface_wind(own, airport: Airport, session: Session):
    """The sim gives wind AT the aircraft. Runway choice and Tower need surface wind, so remember the last
    reading taken low and near the airport, and use it when the aircraft is higher or farther away."""
    near = distance_nm(own.lat, own.lon, airport.lat, airport.lon) <= SURFACE_WIND_RADIUS_NM
    if near and (own.on_ground or own.alt_agl_ft <= SURFACE_WIND_AGL_FT):
        if own.wind_dir_deg is not None and own.wind_kt is not None:
            session.surface_wind = (own.wind_dir_deg, own.wind_kt)
        return own
    if session.surface_wind is not None:
        return replace(own, wind_dir_deg=session.surface_wind[0], wind_kt=session.surface_wind[1])
    return replace(own, wind_dir_deg=None, wind_kt=None)  # winds aloft are not the airport's wind


class _Callbacks:
    """Calls the controller makes on its own: the clearance 10-25 s after "standby". Fired from a timer
    thread; `lock` keeps it from interleaving with a pilot turn."""

    def __init__(self, airport, sim, speaker, history, session, prompt: str = "") -> None:
        self.airport, self.sim, self.speaker, self.history, self.session = airport, sim, speaker, history, session
        self.prompt = prompt  # re-printed after a callback so the text REPL still shows "YOU> "
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None

    def after_turn(self) -> None:
        if self.session.clearance == "standby" and self.timer is None:
            self.timer = threading.Timer(random.uniform(10.0, 25.0), self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _fire(self) -> None:
        with self.lock:
            self.timer = None
            plan = self.session.plan
            facility = resolve_facility(self.airport, self.sim.own().com1_mhz)
            reply = deliver_after_standby(self.session, self.airport, facility, self.session.dest_name or plan.destination_name)
            if reply is None:
                return
            print()
            self.history.append(("(pilot standing by)", reply))
            self.session.last_role = facility.role
            _say_timed(self.speaker, reply, None, 0.0)
            print(self.prompt, end="", flush=True)


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
    elif name == "/final" and len(parts) in (2, 3) and hasattr(sim, "add_on_final"):
        sim.add_on_final(float(parts[1]), f"AI{len(sim.traffic(0, 0, 1e9)) + 1:03d}", parts[2] if len(parts) == 3 else None)
    elif name == "/onrwy" and hasattr(sim, "add_on_runway"):
        sim.add_on_runway(f"AI{len(sim.traffic(0, 0, 1e9)) + 1:03d}", parts[1] if len(parts) == 2 else None)
    elif name == "/notraffic" and hasattr(sim, "clear_traffic"):
        sim.clear_traffic()
    else:
        print("commands: /state /freq <mhz> /wind <dir> <kt> [qnh_hpa] /air /ground /final <nm> [rwy] /onrwy [rwy] "
              "/notraffic /quit (fake sim only)")
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
    p.add_argument("--standby", type=float, default=0.3,
                   help="chance (0-1) Delivery says 'standby' and calls back with the clearance 10-25 s later")
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

    session = Session(callsign=args.callsign, plan=plan, telephony=args.telephony, standby_chance=args.standby)
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
    cb = _Callbacks(airport, sim, speaker, history, session, prompt="YOU> ")
    while True:
        try:
            line = input("YOU> ").lstrip("\ufeff").strip()  # piped input from PowerShell starts with a BOM
        except EOFError:
            break
        if not line:
            continue
        if line.startswith("/"):
            if not _repl_command(line, sim):
                break
            continue
        with cb.lock:
            handle(airport, sim, llm, speaker, history, line, session=session)
        cb.after_turn()


def _stt_hint(airport: Airport, session: Session) -> str:
    """Words speech-to-text should expect on this flight, written the way they should come out."""
    name = airport.spoken_name or airport.name
    words = [f"{name} Delivery, {name} Ground, {name} Tower."]
    num = "".join(c for c in session.callsign if c.isdigit())
    if session.telephony and num:
        words.append(f"{session.telephony} {num}.")
    plan = session.plan
    if plan is not None:
        if plan.sid:
            words.append(f"{phrase.procedure(plan.sid).split()[0]} departure.")
        words.append(f"Cleared to {session.dest_name or plan.destination_name}.")
    return " ".join(words)


def _run_ptt(args, airport, sim, llm, speaker, history, session) -> None:
    from atc.audio.ptt import PushToTalk
    from atc.audio.stt import FasterWhisperSTT

    print(f"loading speech model {args.stt_model} ...")
    stt = FasterWhisperSTT(model_size=args.stt_model)
    ptt = PushToTalk(key=args.ptt_key)
    print(f"{airport.icao} {airport.name}. Hold {args.ptt_key.upper()} to talk. Ctrl+C to exit.")
    cb = _Callbacks(airport, sim, speaker, history, session)
    while True:
        audio = ptt.record_once()
        if len(audio) < 4800:  # under 0.3 s: a tap, not a call
            continue
        t0 = time.perf_counter()
        text = stt.transcribe(audio, hint=_stt_hint(airport, session))
        stt_s = time.perf_counter() - t0
        if not text:
            print("[nothing heard]")
            continue
        print(f"YOU> {text}")
        with cb.lock:
            handle(airport, sim, llm, speaker, history, text, stt_s=stt_s, session=session)
        cb.after_turn()


if __name__ == "__main__":
    main()
