"""Emergencies by code (improvement plan F1): the controller's side of a mayday or a pan pan, after the first reply.

    pilot "Mayday mayday mayday, Martinair 4133, engine failure, request return to the field"
    ATC   "Martinair 4133, roger mayday, runway one three available, wind one four zero degrees one zero knots,
           turn left heading two niner zero, vectors ILS approach runway one three, when able, report persons on board
           and fuel endurance."
    pilot "150 persons, two hours fuel"      -> "roger, emergency services will be standing by."
    pilot "request runway 31"                -> approved whatever the wind (the pilot's choice in an emergency)
    pilot "cancel mayday"                    -> "roger, emergency cancelled."

Intentions understood: return to the field you left, land as soon as possible (the nearest airport on file), divert
to an airport by name or code. The flight's destination becomes that airport (vectors, approach, landing as for any
arrival). Our own traffic gives way: departures there hold, no new arrivals, ours on final close ahead of the
emergency are sent around (own.EMERGENCY, own/manager.py).
"""

from __future__ import annotations

import re

from atc import own as own_traffic
from atc import phrase
from atc.geo import distance_nm
from atc.models import Airport, Facility, OwnState
from atc.readback import _normalize, join_digits
from atc.runway import magnetic, session_runway

_MAYDAY = re.compile(r"\bmay ?day\b")
_PAN = re.compile(r"\bpan ?pan\b")
_RETURN = re.compile(r"\b(?:return(?:ing)?|come back|coming back|turn back|turning back)\b")
_LAND_NOW = re.compile(r"\b(?:immediate landing|land(?:ing)? (?:as soon as possible|asap|immediately|now)|"
                       r"nearest (?:suitable )?(?:airport|airfield|field|aerodrome)|emergency landing|"
                       r"need to land|must land)\b")
_DIVERT = re.compile(r"\bdivert(?:ing|ion)?(?: to)? ([a-z]+)\b")
_SOULS = re.compile(r"\b(\d{1,3}) (?:souls|persons|people|pob|passengers|pax|on board)\b|"
                    r"\b(?:souls|persons|people|pob)(?: on board)? (\d{1,3})\b")
_FUEL = re.compile(r"\b(\d{1,2}) (?:hours?|hrs?|h)(?: (?:and )?(\d{1,2}) (?:minutes?|mins?))?\b|"
                   r"\b(\d{1,3}) (?:minutes?|mins?)(?: of)? (?:fuel|endurance)\b")
_CANCEL = re.compile(r"\bcancel(?:ling|led)? (?:the |our )?(?:mayday|pan ?pan|emergency|distress)\b")
RETURN_MAX_NM = 120.0  # "return": the field the flight left, if it is this close
NEAREST_MAX_NM = 150.0


def handle(session, world, airport: Airport, facility: Facility, own: OwnState, pilot_text: str) -> str | None:
    """The emergency exchange after (and including) the declaration. None = not an emergency call: the usual
    handlers answer it (vectors, approach and landing work as for any arrival)."""
    norm = join_digits(_normalize(pilot_text))
    cs = session.spoken_callsign
    declared = _MAYDAY.search(norm) or _PAN.search(norm)
    if declared:
        kind = "mayday" if _MAYDAY.search(norm) else "pan pan"
        first = session.emergency in (None, "squawk") or (session.emergency == "pan pan" and kind == "mayday")
        session.emergency = kind
        if first:
            session.emergency_info = {}
            return _declared(session, world, airport, facility, own, norm, kind)
    if session.emergency is None:
        return None
    if _CANCEL.search(norm):
        _cancel(session)
        return f"{cs}, roger, emergency cancelled."
    info = session.emergency_info
    bits = []
    intention = _intention(session, world, own, norm)
    if intention is not None and not info.get("airport"):
        bits += _plan_landing(session, world, airport, facility, own, intention)
    souls, fuel = _souls(norm), _fuel(norm)
    if souls is not None:
        info["souls"] = souls
    if fuel is not None:
        info["fuel_min"] = fuel
    if (souls is not None or fuel is not None) and not info.get("services"):
        info["services"] = True
        bits.append("emergency services will be standing by")
    if not bits:
        return None
    if info.get("airport") and not info.get("asked") and "services" not in info:
        info["asked"] = True
        bits.append(_ask_souls(airport))
    return f"{cs}, roger, " + ", ".join(bits) + "."


def _declared(session, world, airport: Airport, facility: Facility, own: OwnState, norm: str, kind: str) -> str:
    """The first reply: "roger mayday", the runway and wind when close to an airport, and the intentions (handled if
    the call had them, else "say intentions"); persons on board and endurance asked once the plan is clear."""
    cs = session.spoken_callsign
    bits = [f"{cs}, roger {kind}"]
    intention = _intention(session, world, own, norm)
    info = session.emergency_info
    souls, fuel = _souls(norm), _fuel(norm)
    if souls is not None:
        info["souls"] = souls
    if fuel is not None:
        info["fuel_min"] = fuel
    if intention is not None and not own.on_ground:
        bits += _plan_landing(session, world, airport, facility, own, intention, first=True)
    else:
        if not own.on_ground and facility.role in ("tower", "approach") and airport.runways:
            rwy = session_runway(airport, own.wind_dir_deg, own.wind_kt, session, "arrival")
            if rwy is not None:
                wind = phrase.wind(magnetic(airport, own.wind_dir_deg), own.wind_kt, airport.faa)
                bits.append(f"runway {phrase.runway(rwy.ident, airport.faa)} available" + (f", {wind}" if wind else ""))
        bits.append("say intentions")
    if info.get("airport") and souls is None and fuel is None:
        info["asked"] = True
        bits.append(_ask_souls(airport))
    elif souls is not None or fuel is not None:
        info["services"] = True
        bits.append("emergency services will be standing by")
    return ", ".join(bits) + "."


def _ask_souls(airport: Airport) -> str:
    return "when able, say souls on board and fuel remaining" if airport.faa else \
        "when able, report persons on board and fuel endurance"


def _intention(session, world, own: OwnState, norm: str) -> Airport | None:
    """The airport the pilot wants to land at: the one they left ("return"), the nearest ("immediate landing"), or
    one named ("divert to Rosario" / "divert to SAAR")."""
    if _RETURN.search(norm):
        dep = world.get(session.departed_from) if session.departed_from else None
        if dep is not None and distance_nm(own.lat, own.lon, dep.lat, dep.lon) <= RETURN_MAX_NM:
            return dep
        return _nearest(world, own)
    m = _DIVERT.search(norm)
    if m:
        word = m.group(1)
        for a in world.airports:
            names = {a.icao.lower(), (a.spoken_name or "").lower(), a.name.split()[0].lower()}
            if word in names:
                return a
    if _LAND_NOW.search(norm):
        return _nearest(world, own)
    return None


def _nearest(world, own: OwnState) -> Airport | None:
    near = [a for a in world.airports if a.runways and distance_nm(own.lat, own.lon, a.lat, a.lon) <= NEAREST_MAX_NM]
    return min(near, key=lambda a: distance_nm(own.lat, own.lon, a.lat, a.lon), default=None)


def _plan_landing(session, world, airport: Airport, facility: Facility, own: OwnState, target: Airport,
                  first: bool = False) -> list[str]:
    """Make `target` the destination (the arrival flow does the rest) and say what comes next: the runway and wind,
    and the first vector toward its final when the position talking is the one that vectors there."""
    from atc.enroute import _arrival_radar, approach_altitude, vectors_text

    plan = session.plan
    info = session.emergency_info
    info["airport"] = target.icao
    if plan is not None and plan.destination != target.icao:
        session.original_destination = session.original_destination or plan.destination
        plan.destination, plan.dest_runway, plan.tod, plan.star = target.icao, None, None, None
        session.dest_name = target.spoken_name or target.name
        session.descent_given = session.center_descent = session.vectors_given = False
        session.intercept_given = session.base_given = session.landing_cleared = False
        session.handoffs_done = {k for k in session.handoffs_done if not k.startswith(f"{target.icao}:")}
        session.landed = False
    rwy = session_runway(target, own.wind_dir_deg, own.wind_kt, session, "arrival")
    own_traffic.EMERGENCY[target.icao] = {"runway": rwy.ident if rwy else None}
    bits = []
    if rwy is not None:
        wind = phrase.wind(magnetic(target, own.wind_dir_deg), own.wind_kt, target.faa)
        bits.append(f"runway {phrase.runway(rwy.ident, target.faa)} available" + (f", {wind}" if wind else ""))
    if plan is None or rwy is None:
        return bits
    radar_here = airport.icao == target.icao and _arrival_radar(world, session, airport, facility) or \
        facility.role in ("approach", "departure") and distance_nm(own.lat, own.lon, target.lat, target.lon) <= 40
    if radar_here and distance_nm(own.lat, own.lon, target.lat, target.lon) <= 40:
        alt = approach_altitude(target, rwy, own, session)
        session.descent_given = True
        if own.alt_msl_ft > alt + 300:
            session.assign_level(alt, own.alt_msl_ft)
            bits.append(f"{phrase.descend(alt, target)} at your discretion" if not target.faa else
                        f"{phrase.descend(alt, target)}, at pilot's discretion")
        elif session.cleared_level_ft is None or session.cleared_level_ft < alt:
            session.assign_level(alt, own.alt_msl_ft)
        vec = vectors_text(session, target, rwy, own)
        bits.append(vec.split(", ", 1)[1].rstrip("."))
    else:
        bits.append(f"cleared direct {target.spoken_name or target.name}")
    return bits


def _souls(norm: str) -> int | None:
    m = _SOULS.search(norm)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _fuel(norm: str) -> int | None:
    """Fuel endurance in minutes ("two hours fuel", "1 hour 30 minutes", "45 minutes of fuel")."""
    m = _FUEL.search(norm)
    if not m:
        return None
    if m.group(3):
        return int(m.group(3))
    return int(m.group(1)) * 60 + int(m.group(2) or 0)


def _cancel(session) -> None:
    session.emergency = None
    session.emergency_info = {}
    own_traffic.EMERGENCY.clear()


def landed(session, icao: str) -> None:
    """The user is down at `icao` (flow.track): our traffic there goes back to normal."""
    if session.emergency is not None:
        own_traffic.EMERGENCY.pop(icao, None)


def context_lines(session) -> list[str]:
    """Facts for the model when an emergency is on (a free-form question in the middle of it)."""
    if session.emergency is None:
        return []
    what = {"mayday": "MAYDAY (distress)", "pan pan": "PAN PAN (urgency)",
            "squawk": "transponder 7700 (emergency squawk)"}[session.emergency]
    info = session.emergency_info or {}
    known = []
    if info.get("airport"):
        known.append(f"landing at {info['airport']}")
    if info.get("souls") is not None:
        known.append(f"{info['souls']} persons on board")
    if info.get("fuel_min") is not None:
        known.append(f"fuel endurance {info['fuel_min']} minutes")
    return [f"EMERGENCY declared by the pilot: {what}" + (f" ({', '.join(known)})" if known else "") +
            ". Give them priority, keep instructions short, never refuse a request on the grounds of traffic; "
            "software handles the runway, vectors and the persons-on-board question."]
