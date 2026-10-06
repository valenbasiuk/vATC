"""Main loop. Text mode first (type what you would say); voice plugs into the same `handle()`.

    python -m atc --airport SARC            # fake sim, type your calls
    python -m atc --airport SARC --sim      # real MSFS via SimConnect (add --dll PATH if needed)
    python -m atc --airport SARC --voice m.onnx   # speak replies with Piper (Phase 3)

REPL commands (fake sim only):  /state  /freq 118.1  /wind 270 8 1013  /air  /ground
                                /final 3 [31] (AI arrival on 3 NM final)  /onrwy [31]  /notraffic  /quit
                                /aidep [31] (AI that departs)  /aiarr 8 [31] (AI that lands from 8 NM out)
                                /leg downwind|base|final [nm]|upwind|out (own aircraft in the VFR circuit)
"""

from __future__ import annotations

import argparse
import random
import re
import threading
import time
from dataclasses import replace
from pathlib import Path

from atc import atis, enroute, factcheck, flow, monitor, pattern, phrase, weather
from atc.clearance import context_lines, deliver_after_standby, handle_clearance, handle_push
from atc.facility import callsign_for
from atc.flightplan import load_simbrief
from atc.flow import handle_flow
from atc.geo import distance_nm
from atc.llm.client import make_llm
from atc.llm.prompt import build_context, build_messages, build_system_prompt
from atc.models import Airport
from atc.readback import _normalize, check_readback, is_acknowledgement, readback_missing
from atc.runway import runway_in_use
from atc.sequence import context_lines as sequence_context
from atc.sequence import guard, runway_status
from atc.session import Session
from atc.sim.base import SimSource
from atc.chatter import RadioBus, exchange_for
from atc.taxi import departure_route, handle_crossing, handle_taxi
from atc.tracker import TrafficTracker
from atc.traffic import is_traffic_question, traffic_reply
from atc.world import World, load_world

TRAFFIC_RADIUS_NM = 15.0
ATIS_PAUSE_S = 3.0  # between two loops of the ATIS broadcast
ATIS_TEXT_PAUSE_S = 60.0  # text mode: print it once a minute, not in a loop
ATIS_VOICE = 12  # libritts speaker for the ATIS, not the controller's voice


def handle(
    airport: Airport,
    sim: SimSource,
    llm,
    speaker,
    history: list,
    pilot_text: str,
    stt_s: float | None = None,
    session: Session | None = None,
    world: World | None = None,
) -> str | None:
    """One pilot transmission -> one ATC reply (or None if nobody answers).
    With a `world`, the tuned frequency picks the airport (origin, destination, area control...)."""
    own = sim.own()
    world = world or World([airport])
    picked = world.pick(own)
    if picked is None:
        print(f"[no station on {own.com1_mhz:.3f}]")
        return None
    airport, facility = picked
    if not facility.can_reply:
        print(f"[{facility.role} on {own.com1_mhz:.3f}: nobody answers here]")
        return None
    if session is None:
        session = Session(callsign=own.callsign)
    session.where = airport.icao
    session.faa = airport.faa
    own = _surface_wind(own, airport, session)
    session.learn_telephony(pilot_text)
    cs = session.spoken_callsign
    station = callsign_for(airport, facility)
    plan = session.plan
    preferred = (plan.planned_runway if plan and plan.origin == airport.icao and own.on_ground
                 else plan.dest_runway if plan and plan.destination == airport.icao else None)

    # first call on a position with an old ATIS letter: "information Charlie is now current, QNH ..."
    atis_note = atis.check_letter(session.atis, airport, own, _normalize(pilot_text),
                                  session.surface_wind.get(airport.icao), preferred) \
        if session.is_first_contact(facility.role) and facility.role in ("clearance", "ground", "tower", "approach") \
        else None

    def say(reply: str, llm_s: float = 0.0) -> str:
        if atis_note and "now current" not in reply:
            reply = reply.rstrip(".") + f". {atis_note[:1].upper()}{atis_note[1:]}."
        history.append((pilot_text, reply))
        session.last_role = session._key(facility.role)
        _say_timed(speaker, reply, stt_s, llm_s, _atc_voice(speaker, airport, facility.role))
        return reply

    reply = None
    if session.names_other_flight(pilot_text):  # misheard or wrong callsign: never answer it as ours
        reply = f"Station calling {station}, say again your callsign."
    if reply is None:  # mayday / pan pan / going around come before anything else
        reply = monitor.handle_pilot(session, world, airport, facility, own, pilot_text, time.monotonic())
    if reply is None:  # "Rosario Center ..." on Aeroparque Delivery: "this is Aeroparque Delivery, ..."
        reply = flow.wrong_station(session, world, airport, facility, own, pilot_text)
    if reply is None:  # "requesting Delivery frequency" / "frequency change": from the files, never invented
        reply = flow.frequency_request(session, world, airport, facility, own, pilot_text)
    if reply is None and plan is not None:  # IFR clearance is code's job: issue, readback check, correction
        reply = handle_clearance(session, airport, facility, pilot_text, session.dest_name or plan.destination_name)
    if reply is None:
        reply = handle_push(session, airport, facility, pilot_text)
    if reply is None:  # "on holding point runway 31" to Ground: "contact Tower ..."
        reply = flow.at_holding_point(session, airport, facility, own, pilot_text)
    traffic = sim.traffic(airport.lat, airport.lon, TRAFFIC_RADIUS_NM)
    if reply is None:  # VFR circuit, before the readback check ("left downwind 31" repeats Tower's own words)
        reply = pattern.handle(session, airport, facility, own, pilot_text, traffic, preferred)
    if reply is None:  # "holding short of runway 28R": cross it or keep holding (also before the readback check)
        reply = handle_crossing(session, airport, facility, own, pilot_text, traffic)
    # A readback only answers the position that gave the instruction: after "contact Tower", the first call
    # on Tower is a new call even if it repeats Ground's "holding point runway 31".
    last_atc = history[-1][1] if history and session.last_role in (None, session._key(facility.role)) else None
    key = session._key(facility.role)
    # What this call is checked against: an instruction ATC asked to have read back, else the last instruction
    # unless it was already read back correctly (later calls like "on holding point Alfa" are not readbacks).
    target = session.readback_due.get(key) or (last_atc if last_atc != session.acked_atc else None)
    rb = check_readback(target, pilot_text)
    words = tuple(cs.split())
    if reply is None and rb.status == "none" and readback_missing(target, pilot_text, words):
        session.readback_due[key] = target  # "roger" to a taxi/takeoff/QNH/... instruction: ICAO "read back"
        reply = f"{cs}, read back."
    elif reply is None and (
        rb.status == "correct" or (rb.status == "none" and is_acknowledgement(last_atc, pilot_text, words))
    ):
        # Decided by code, no LLM call. Real controllers don't answer a correct readback or a "roger".
        session.acked_atc = last_atc
        session.readback_due.pop(key, None)
        print("[ATC: no reply needed]")
        return None
    elif reply is None and rb.status == "incomplete" and target:
        # Wrong readback: say the instruction again, word for word (ICAO "negative, I say again").
        session.readback_due.pop(key, None)  # the correction itself now carries the instruction
        contact = re.search(r"contact [a-z ]+?(?:decimal|point)(?: (?:zero|one|two|three|four|five|six|seven|eight|"
                            r"niner))+", target, re.I)
        if rb.missing == ["frequency"] and contact:  # wrong frequency read back: "negative, contact Tower 118.85"
            reply = f"{cs}, negative, {contact.group(0)}."
        else:
            reply = f"{cs}, negative, I say again, {_instruction(target, cs, station)}"
    if reply is not None:
        return say(reply)

    # Fixed-form calls with facts from code: traffic information, taxi, check-ins, takeoff/landing.
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt, preferred,
                        use="departure" if own.on_ground and not session.landed else "arrival")
    if is_traffic_question(_normalize(pilot_text), pilot_text):
        reply = traffic_reply(cs, own, sim.traffic(own.lat, own.lon, TRAFFIC_RADIUS_NM))
    if reply is None:
        reply = handle_taxi(session, airport, facility, own, pilot_text, world.taxi.get(airport.icao), traffic, rwy)
    if reply is None:
        reply = handle_flow(session, world, airport, facility, own, pilot_text, traffic, preferred)
    if reply is None:  # direct to / higher / descent / vectors, from the SimBrief route and the telemetry
        reply = enroute.handle_request(session, world, airport, facility, own, pilot_text)
    if reply is not None:
        return say(reply)

    context = build_context(own, traffic, airport, cs, facility.role, session.first_contact(facility.role),
                            preferred_runway=preferred)
    if facility.role == "ground" and own.on_ground and rwy is not None:
        via = departure_route(world.taxi.get(airport.icao), airport, rwy, own)
        if via and not airport.taxi_routes.get(rwy.ident):
            context = context.replace(
                "No taxi route on file: give the taxi clearance without naming any taxiway.",
                f"TAXI ROUTE to runway {rwy.ident} (computed from the airport map, treat as fact): via {via}")
    plan_lines = context_lines(session, airport, facility) + monitor.context_lines(session) + \
        pattern.context_lines(session) + pattern.transit_lines(pilot_text)
    if plan and plan.is_ifr and session.clearance == "confirmed" and own.squawk != plan.squawk \
            and facility.role in ("tower", "departure", "approach"):
        plan_lines.append(f"  TRANSPONDER WRONG: the pilot squawks {own.squawk}, assigned {plan.squawk}. "
                          f"Start your reply with '{cs}, squawk {phrase.digits(plan.squawk)}'.")
    status = None
    if facility.role == "tower" and rwy is not None:  # who may take off / land is code's decision
        status = runway_status(airport, rwy, own, traffic)
        plan_lines += sequence_context(status, own)
    if plan_lines:
        context += "\n" + "\n".join(plan_lines)
    system = build_system_prompt(airport, facility)
    messages = build_messages(system, context, history, pilot_text)
    t_llm = time.perf_counter()
    try:
        reply = llm.complete(messages)
        sources = [system, context, pilot_text] + [a for _, a in history[-6:]]
        stations = world.stations()

        def check(text: str) -> list[str]:
            return factcheck.problems(text, sources) + factcheck.contact_problems(text, stations)

        found = check(reply)
        if found:  # it stated something it wasn't told: one retry with the problem named, then a safe reply
            print(f"[fact check rejected: {reply} ({', '.join(found)})]")
            retry = messages + [{"role": "assistant", "content": reply},
                                {"role": "user", "content": factcheck.correction(found)}]
            reply = llm.complete(retry)
            if check(reply):
                print(f"[fact check rejected again: {reply}]")
                reply = f"{cs}, say again."
    except Exception as exc:  # network, rate limit, bad model: never crash the session
        print(f"[LLM error, nobody answers: {str(exc)[:200]}]")
        return None
    llm_s = time.perf_counter() - t_llm
    if re.fullmatch(rf"{re.escape(cs)}[,\s]*(?:roger|copied|wilco)[.\s]*", reply, re.I):
        print(f"[ATC: no reply needed (model only said: {reply})]")  # real controllers don't answer a roger
        return None
    safe = guard(reply, status, cs, own)
    if safe != reply:
        print(f"[sequence guard replaced: {reply}]")
        reply = safe
    return say(reply, llm_s)


def _instruction(last_atc: str, cs: str, station: str) -> str:
    """The instruction itself, without callsign, station name or an earlier 'negative, I say again'
    (so a second correction doesn't become 'negative, I say again, negative, I say again, ...')."""
    text = last_atc.removeprefix(cs).lstrip(" ,")
    while True:
        stripped = re.sub(r"^(?:negative,?\s*)?i say again,?\s*", "", text, flags=re.I)
        stripped = stripped.removeprefix(station).lstrip(" ,")
        stripped = re.sub(r",?\s*good day\.?$", ".", stripped, flags=re.I)  # "negative ..., contact X 118.85."
        if stripped == text:
            return text
        text = stripped


SURFACE_WIND_AGL_FT = 3000.0
SURFACE_WIND_RADIUS_NM = 15.0  # a VFR inbound calls Tower ~10 NM out, below 3000 ft: close enough to surface wind


def _surface_wind(own, airport: Airport, session: Session):
    """The sim gives wind AT the aircraft. Runway choice and Tower need surface wind, so remember the last
    reading taken low and near the airport, and use it when the aircraft is higher or farther away."""
    near = distance_nm(own.lat, own.lon, airport.lat, airport.lon) <= SURFACE_WIND_RADIUS_NM
    if near and (own.on_ground or own.alt_agl_ft <= SURFACE_WIND_AGL_FT):
        if own.wind_dir_deg is not None and own.wind_kt is not None:
            session.surface_wind[airport.icao] = (own.wind_dir_deg, own.wind_kt)
        return own
    known = session.surface_wind.get(airport.icao)
    if known is not None:
        return replace(own, wind_dir_deg=known[0], wind_kt=known[1])
    metar = weather.get(airport.icao)  # arriving: the airport's real surface wind (what live weather shows)
    if metar is not None and metar.wind_kt is not None:
        return replace(own, wind_dir_deg=metar.wind_dir_deg, wind_kt=metar.wind_kt)
    return replace(own, wind_dir_deg=None, wind_kt=None)  # winds aloft are not the airport's wind


class _Callbacks:
    """Calls the controller makes on its own, from a background thread:
    - the clearance 10-25 s after "standby" (timer);
    - handoffs from telemetry (flow.next_handoff), checked every `tick_s`, said again once if ignored.
    `lock` keeps them from interleaving with a pilot turn; nothing is said while the PTT key is held."""

    def __init__(self, world, sim, speaker, history, session, prompt: str = "", ptt=None, tick_s: float = 1.0) -> None:
        if isinstance(world, Airport):
            world = World([world])
        self.world, self.sim, self.speaker, self.history, self.session = world, sim, speaker, history, session
        self.prompt = prompt  # re-printed after a callback so the text REPL still shows "YOU> "
        self.ptt = ptt
        self.tick_s = tick_s
        self.lock = threading.Lock()
        self.timer: threading.Timer | None = None
        self.stop = threading.Event()
        self.bus = RadioBus()  # one frequency: chatter waits for gaps and for the user's turn
        self.trackers: dict[str, TrafficTracker] = {}
        self.last_chatter: list[tuple[str, str]] = []  # (who, text) of the last exchange, for tests
        self.atis_play: dict | None = None  # the ATIS being broadcast: airport, sentences, position

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="atc-watcher").start()

    def after_turn(self, replied: bool = True) -> None:
        """After every pilot call: a gap on the frequency, and the user's turn if ATC answered them."""
        self.bus.heard(time.monotonic(), to_user=replied)
        if self.session.clearance == "standby" and self.timer is None:
            self.timer = threading.Timer(random.uniform(10.0, 25.0), self._fire)
            self.timer.daemon = True
            self.timer.start()

    def _loop(self) -> None:
        while not self.stop.wait(self.tick_s):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the watcher must never kill the radio
                print(f"[watcher: {exc}]")

    def tick(self, now: float | None = None) -> str | None:
        """One watcher step. Returns what the controller said (for tests)."""
        if self.ptt is not None and self.ptt.talking.is_set():
            return None
        with self.lock:
            now = time.monotonic() if now is None else now
            if hasattr(self.sim, "step"):  # fake sim: move the scripted AI traffic
                self.sim.step(now)
            own = self.sim.own()
            here = self.world.pick(own)
            if here is not None:
                self.session.faa = here[0].faa  # FAA or ICAO callsign form for what the controller says now
            near = self.world.nearest(own)
            if near is not None:  # keep the airport's surface wind current (ATIS, chatter, runway in use)
                _surface_wind(own, near, self.session)
            flow.track(self.session, self.world, own, now)
            text = flow.squawk_now_correct(self.session, own) or flow.repeat_or_clear(self.session, own, now)
            if text is None and self.session.pending_handoff is None:
                due = flow.next_handoff(self.session, self.world, own)
                if due is not None:
                    key, body = due
                    self.session.handoffs_done.add(key)
                    self.session.pending_handoff = flow.Pending(key, body, own.com1_mhz, now)
                    text = f"{self.session.spoken_callsign}, {body}."
            if text is None and self.session.pending_handoff is None:  # level bust, 7700, go-around
                text = monitor.radar_event(self.session, self.world, own,
                                           self.sim.traffic(own.lat, own.lon, TRAFFIC_RADIUS_NM), now)
            if text is None and self.session.pending_handoff is None:  # VFR circuit: downwind again, zone left
                text = pattern.watcher_event(self.session, self.world, own,
                                             self.sim.traffic(own.lat, own.lon, TRAFFIC_RADIUS_NM))
            if text is None and self.session.pending_handoff is None:
                # descent at top of descent, vectors, intercept, landing clearance, vacate
                text = enroute.arrival_event(self.session, self.world, own,
                                             self.sim.traffic(own.lat, own.lon, TRAFFIC_RADIUS_NM))
            if text is not None:
                self._transmit(text, "(controller call)")
                self.bus.heard(now, to_user=True)
                return text
            if not self._chatter(own, now):
                self._atis(own, now)
            return None

    def _atis(self, own, now: float) -> str | None:
        """One sentence of the ATIS per tick while COM1 (or a COM2 that is heard) is on an ATIS frequency, so
        pilot calls and controller calls on COM1 still get through between sentences."""
        found = None
        for mhz in (own.com1_mhz, own.com2_mhz):
            if mhz is None:
                continue
            for a in self.world.airports:
                if any(f.kind in ("ATIS", "AWOS", "ASOS") and abs(f.mhz - mhz) < 0.005 for f in a.frequencies):
                    found = a
                    break
            if found:
                break
        if found is None:
            self.atis_play = None
            return None
        p = self.atis_play
        if p is None or p["icao"] != found.icao:
            p = self.atis_play = {"icao": found.icao, "lines": [], "i": 0, "next": now}
            weather.get(found.icao, wait_s=4.0)  # the first broadcast should have the clouds and temperature
        if now < p["next"]:
            return None
        if p["i"] >= len(p["lines"]):  # (re)start the broadcast: the letter may have changed meanwhile
            plan = self.session.plan
            pref = plan.planned_runway if plan and plan.origin == found.icao and own.on_ground else \
                plan.dest_runway if plan and plan.destination == found.icao else None
            _, p["lines"] = atis.build(self.session.atis, found, own, self.session.surface_wind.get(found.icao), pref)
            p["i"] = 0
        line = p["lines"][p["i"]]
        p["i"] += 1
        if p["i"] >= len(p["lines"]):  # pause before it starts over (long in text mode: no audio to loop)
            p["next"] = now + (ATIS_PAUSE_S if getattr(self.speaker, "audio", False) else ATIS_TEXT_PAUSE_S)
        say_as = getattr(self.speaker, "say_as", None)
        if say_as is not None:
            say_as(line, who="ATIS", voice=_atc_voice(self.speaker, found, "atis") or ATIS_VOICE)
        else:
            self.speaker.say(line)
        return line

    def _chatter(self, own, now: float) -> bool:
        """Radio traffic with the AI aircraft at the airport we're at: track them, queue what ATC would say,
        and say it when the frequency is free and it is not the user's turn."""
        apt = self.world.nearest(own)
        if apt is None or distance_nm(own.lat, own.lon, apt.lat, apt.lon) > 25:
            return False
        traffic = self.sim.traffic(apt.lat, apt.lon, TRAFFIC_RADIUS_NM)
        tracker = self.trackers.setdefault(apt.icao, TrafficTracker(apt))
        wind = self.session.surface_wind.get(apt.icao) or (own.wind_dir_deg, own.wind_kt)
        for ev in tracker.update(traffic, now):
            ex = exchange_for(ev, own, traffic, wind, self.world.taxi.get(apt.icao))
            if ex is not None:
                self.bus.offer(ex)
        picked = self.world.pick(own)
        role = picked[1].role if picked and picked[0].icao == apt.icao and picked[1].can_reply else None
        ex = self.bus.next_due(now, role)
        if ex is None:
            return False
        if "cleared to land" in ex.lines[0][1] and ex.key in tracker.tracks:
            tracker.tracks[ex.key].cleared = True  # said now: stop offering it again
        print()
        self.last_chatter = []
        bank = getattr(self.speaker, "bank", None)
        for who, text, voice in ex.lines:
            if bank is not None:  # the controller of that position, and the AI pilot with its country's accent
                voice = bank.controller(apt.icao, ex.role, apt.country) if who == "ATC" else \
                    bank.pilot(ex.key, ex.country) or voice
            say_as = getattr(self.speaker, "say_as", None)
            if say_as is not None:
                say_as(text, who=who, voice=voice)
            else:
                self.speaker.say(text)
            self.last_chatter.append((who, text))
        print(self.prompt, end="", flush=True)
        self.bus.heard(now)
        return True

    def _transmit(self, text: str, pilot_side: str) -> None:
        picked = self.world.pick(self.sim.own())
        if picked is not None:
            self.session.where = picked[0].icao
            self.session.last_role = self.session._key(picked[1].role)
        print()
        self.history.append((pilot_side, text))
        _say_timed(self.speaker, text, None, 0.0,
                   _atc_voice(self.speaker, picked[0], picked[1].role) if picked else None)
        print(self.prompt, end="", flush=True)

    def _fire(self) -> None:
        with self.lock:
            self.timer = None
            plan = self.session.plan
            picked = self.world.pick(self.sim.own())
            airport = picked[0] if picked else self.world.airports[0]
            facility = picked[1] if picked else None
            reply = deliver_after_standby(self.session, airport, facility, self.session.dest_name or plan.destination_name)
            if reply is not None:
                self._transmit(reply, "(pilot standing by)")


def _atc_voice(speaker, airport: Airport | None, role: str | None):
    """The voice of that controller position (its own person, with the airport country's accent), if the speaker
    has a voice bank."""
    bank = getattr(speaker, "bank", None)
    if bank is None or airport is None or role is None:
        return None
    return bank.controller(airport.icao, role, airport.country)


def _say_timed(speaker, reply: str, stt_s: float | None, llm_s: float, voice=None) -> None:
    """Speak, then print per-stage latency (roadmap item 15). Target: under 3-4 s to first audio."""
    if voice is not None and getattr(speaker, "audio", False):
        speaker.say_as(reply, who="ATC", voice=voice)
    else:
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


def _repl_command(cmd: str, sim, world: World | None = None) -> bool:
    """Returns False to quit."""
    parts = cmd.split()
    name = parts[0]
    if name == "/quit":
        return False
    if name == "/state":
        print(sim.own())
    elif name == "/near" and len(parts) in (3, 4) and world is not None and hasattr(sim, "place_on_final"):
        target = world.get(parts[1])
        if target is None:
            print(f"{parts[1]} is not loaded (only the plan's airports and --airport are)")
        else:
            sim.place_on_final(target, float(parts[2]), float(parts[3]) if len(parts) == 4 else None)
    elif name == "/leg" and len(parts) in (2, 3) and parts[1] in ("downwind", "base", "final", "upwind", "out") \
            and hasattr(sim, "place_on_leg"):  # VFR circuit of the airport you're at
        apt = (world.nearest(sim.own()) if world is not None else None) or sim._airport
        sim.place_on_leg(apt, parts[1], float(parts[2]) if len(parts) == 3 else None)
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
        sim._scripts.clear()
    elif name in ("/aidep", "/aiarr") and hasattr(sim, "spawn_departure"):
        # moving AI traffic: "/aidep [rwy]" departs from the holding point, "/aiarr [nm] [rwy]" lands
        n = len(sim._scripts) + len(sim.traffic(0, 0, 1e9)) + 1
        info = dict(airline="Aerolineas Argentinas", flight_number=str(1200 + n * 11), type="A320")
        args = parts[1:]
        if name == "/aidep":
            sim.spawn_departure(f"ARG{1200 + n * 11}", args[0] if args else None, time.monotonic(), **info)
        else:
            nm = float(args[0]) if args else 8.0  # first seen outside 6 NM, so it gets its "cleared to land"
            sim.spawn_arrival(f"ARG{1200 + n * 11}", nm, args[1] if len(args) > 1 else None, time.monotonic(),
                              **info)
    else:
        print("commands: /state /freq <mhz> /wind <dir> <kt> [qnh_hpa] /air /ground /final <nm> [rwy] /onrwy [rwy] "
              "/aidep [rwy] /aiarr [nm] [rwy] /notraffic /near <ICAO> <nm> [alt_ft] /leg <downwind|base|final|upwind|out> "
              "/quit (fake sim only)")
    return True


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="atc")
    p.add_argument("--airport", help="ICAO code; file is airports/<ICAO>.yaml (with --sim: default = where you are)")
    p.add_argument("--airports-dir", type=Path, default=Path("airports"))
    p.add_argument("--sim", action="store_true", help="use real MSFS (default: fake sim)")
    p.add_argument("--voice", type=Path, help="Piper .onnx voice file; omit for text only")
    p.add_argument("--ptt", action="store_true", help="push-to-talk input (hold the key, speak, release)")
    p.add_argument("--ptt-key", default="f9", help='keyboard key ("f9", "f10", "v"...; "none" = joystick only)')
    p.add_argument("--ptt-joy", type=int, help="joystick/yoke button number for push-to-talk (tools/probe_ptt.py)")
    p.add_argument("--ptt-joy-device", type=int, help="which game controller (default: any)")
    p.add_argument("--mic", help="microphone: sounddevice index or part of its name (default: Windows default)")
    p.add_argument("--audio-out", help="speakers/headset for ATC: index or part of the name (default: Windows)")
    p.add_argument("--stt-model", default="small.en", help="faster-whisper model (try base.en if too slow)")
    p.add_argument("--callsign", help="ICAO callsign (default: SimBrief plan, else the sim's ATC settings, else N123AB)")
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

    sim: SimSource | None = None
    if args.sim:
        from atc.sim.simconnect_source import SimConnectSource

        sim = SimConnectSource(callsign=args.callsign or "N123AB", library_path=args.dll)
        if args.callsign is None:  # no plan, no --callsign: what the sim's ATC calls the aircraft
            from atc.session import callsign_from_sim

            cs, tel = callsign_from_sim(sim.identity())
            if cs:
                args.callsign, args.telephony = cs, args.telephony or tel
                sim._callsign = cs
                print(f"callsign from the sim: {cs}" + (f" ({tel})" if tel else ""))
    args.callsign = args.callsign or "N123AB"

    primary = args.airport or (plan.origin if plan else None)
    if primary is None and sim is None:
        raise SystemExit("--airport is needed with the fake sim (with --sim the airport you are at is found)")
    world = load_world(primary, args.airports_dir, plan)
    if sim is not None and (args.airport is None or not world.airports):
        here = world.discover(sim.own())  # spawned somewhere: that airport first (written to airports/ if new)
        if here is not None:
            world.airports.remove(here)
            world.airports.insert(0, here)
    if not world.airports:
        raise SystemExit("Can't tell which airport you're at (sim not on the ground at an airport?). Use --airport.")
    airport = world.airports[0]
    for a in world.airports:
        net = world.taxi.get(a.icao)
        print(f"{a.icao}: {'taxi map from ' + net.source if net else 'no taxi map (tools/fetch_osm_taxi.py ' + a.icao + ')'}"
              + (", needs_review: true" if a.needs_review else ""))
    if world.airspaces:
        print("area control: " + ", ".join(s.icao for s in world.airspaces))

    if sim is None:
        from atc.sim.fake import FakeSim

        sim = FakeSim(airport, callsign=args.callsign)
        stand = _a_stand(world, airport)
        if stand is not None:  # start on a stand, not at the reference point (often on the runway)
            sim.update(lat=stand[1][0], lon=stand[1][1])
            print(f"fake sim: parked on stand {stand[0]}")

    if args.voice:
        from atc.audio.tts import PiperTTS

        from atc.voices import VoiceBank

        bank = VoiceBank(args.voice.parent, args.voice)
        print(f"voices: {bank.describe()}" + ("" if len(bank.pools) > 2 else
                                               "  (more accents: python tools/download_voices.py)"))
        speaker = PiperTTS(args.voice, device=_device(args.audio_out), bank=bank)
        models = {v.model for pool in bank.pools.values() for v in pool}
        threading.Thread(target=speaker.preload, args=(sorted(models),), daemon=True, name="voices").start()
    else:
        from atc.audio.tts import PrintTTS

        speaker = PrintTTS()

    import atc.session as session_mod

    session_mod.LEARNED_PATH = Path("data/telephony_learned.json")  # MAR -> Martinair, remembered between flights
    session = Session(callsign=args.callsign, plan=plan, telephony=args.telephony, standby_chance=args.standby)
    if session.telephony:
        print(f"telephony: {session.spoken_callsign} ({'--telephony' if session.telephony_source == 'given' else 'remembered / airline list; your first call can change it'})")
    dest = world.get(plan.destination) if plan else None
    if dest is not None:  # its spoken_name is how the clearance limit is said ("Rosario")
        session.dest_name = dest.spoken_name or dest.name

    for a in world.airports:  # METARs in the background: ATIS, and the surface wind of the destination
        weather.get(a.icao)
    llm = make_llm()
    print(f"LLM: {getattr(llm, 'model', 'stub (no ATC_LLM_MODEL set)')}")
    history: list[tuple[str, str]] = []
    try:
        if args.ptt:
            _run_ptt(args, world, sim, llm, speaker, history, session)
        else:
            _run_text(world, sim, llm, speaker, history, session)
    except KeyboardInterrupt:
        pass
    finally:
        sim.close()


def _run_text(world, sim, llm, speaker, history, session) -> None:
    airport = world.airports[0]
    print(f"{airport.icao} {airport.name}. Type your radio calls. /quit to exit.")
    cb = _Callbacks(world, sim, speaker, history, session, prompt="YOU> ")
    cb.start()
    while True:
        try:
            line = input("YOU> ").lstrip("\ufeff").strip()  # piped input from PowerShell starts with a BOM
        except EOFError:
            break
        if not line:
            continue
        if line.startswith("/"):
            with cb.lock:
                if not _repl_command(line, sim, world):
                    break
            continue
        with cb.lock:
            reply = handle(airport, sim, llm, speaker, history, line, session=session, world=world)
        cb.after_turn(replied=reply is not None)


def _device(spec: str | None):
    """--mic / --audio-out: an index ("3") or part of the device name ("Headset"), as sounddevice takes it."""
    if spec is None:
        return None
    return int(spec) if spec.isdigit() else spec


def _a_stand(world: World, airport: Airport):
    """(stand ref, (lat, lon)) of a stand off the runways, nearest the airport reference point; None without a map."""
    from atc.sequence import along_cross

    net = world.taxi.get(airport.icao)
    if net is None or not net.stands:
        return None
    best = None
    for ref, node in net.stands.items():
        lat, lon = net.nodes[node]
        if any(along_cross(airport, r, lat, lon)[1] < 0.08 for r in airport.runways):
            continue
        d = distance_nm(lat, lon, airport.lat, airport.lon)
        if best is None or d < best[0]:
            best = (d, ref, (lat, lon))
    return (best[1], best[2]) if best else None


def _stt_words(world: World, session: Session) -> list[str]:
    """Names speech-to-text should expect on this flight, written the way they should come out."""
    words = []
    for a in world.airports:
        name = a.spoken_name or a.name.split()[0]
        words.append(f"{name} Delivery, {name} Ground, {name} Tower, {name} Approach.")
        spoken = sorted({f.spoken for f in a.frequencies if f.spoken})  # "NorCal Departure", "Oakland Center"
        if spoken:
            words.append(", ".join(spoken[:6]) + ".")
    for s in world.airspaces:
        words.append(f"{s.spoken_name or s.name} Control.")
    num = "".join(c for c in session.callsign if c.isdigit())
    if session.telephony and num:
        words.append(f"{session.telephony} {num}.")
    plan = session.plan
    if plan is not None:
        if plan.sid:
            words.append(f"{phrase.procedure(plan.sid).split()[0]} departure.")
        words.append(f"Cleared to {session.dest_name or plan.destination_name}.")
    return words


def _stt_hint(world: World | Airport, session: Session) -> str:
    return " ".join(_stt_words(world if isinstance(world, World) else World([world]), session))


def _hotwords(world: World, session: Session) -> str:
    names = {session.telephony or ""} | {a.spoken_name or "" for a in world.airports}
    if session.plan and session.plan.sid:
        names.add(phrase.procedure(session.plan.sid).split()[0])
    return " ".join(sorted(n for n in names if n))


def _run_ptt(args, world, sim, llm, speaker, history, session) -> None:
    from atc.audio.ptt import PushToTalk
    from atc.audio.stt import FasterWhisperSTT

    airport = world.airports[0]
    print(f"loading speech model {args.stt_model} ...")
    stt = FasterWhisperSTT(model_size=args.stt_model)
    ptt = PushToTalk(key=args.ptt_key, joy_button=args.ptt_joy, joy_device=args.ptt_joy_device,
                     mic=_device(args.mic))
    print(f"{airport.icao} {airport.name}. Hold {ptt.describe()} to talk. Ctrl+C to exit.")
    cb = _Callbacks(world, sim, speaker, history, session, ptt=ptt)
    cb.start()
    while True:
        audio = ptt.record_once()
        if len(audio) < 4800:  # under 0.3 s: a tap, not a call
            continue
        t0 = time.perf_counter()
        text = stt.transcribe(audio, hint=_stt_hint(world, session), hotwords=_hotwords(world, session))
        stt_s = time.perf_counter() - t0
        if not text:
            print("[nothing heard]")
            continue
        print(f"YOU> {text}")
        with cb.lock:
            reply = handle(airport, sim, llm, speaker, history, text, stt_s=stt_s, session=session, world=world)
        cb.after_turn(replied=reply is not None)


if __name__ == "__main__":
    main()
