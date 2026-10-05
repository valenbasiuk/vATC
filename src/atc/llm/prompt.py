"""Prompt assembly. This file is where most of the quality work will happen (Phase 4+)."""

from __future__ import annotations

from atc.facility import callsign_for, describe_frequencies
from atc.geo import bearing_deg, compass_point, distance_nm
from atc.models import Airport, Facility, OwnState, Traffic
from atc.runway import runway_in_use

SYSTEM_TEMPLATE = """\
You are {facility_name}, an air traffic controller in a flight simulator. You are answering a pilot \
on {freq:.3f} MHz as the {role} position.

HARD RULES
1. Reply with ONLY the words to be spoken on the radio. No markdown, no quotes, no explanations.
2. Use standard {phraseology} phraseology. Be brief: one to three short sentences. Start every reply with the \
pilot's spoken callsign exactly as CONTEXT gives it. Say your own station name only when CONTEXT says "first contact".
2b. Say numbers the way they are said on the radio: {number_rule}
2c. Never say "standby" and never promise to call back: answer the request now, or say "unable".
3. Use ONLY the runways, frequencies, taxi routes and traffic listed under CONTEXT. Name taxiways only if CONTEXT \
has a TAXI ROUTE line, and then exactly as given; never invent a taxiway, gate or intersection. Never mention a \
runway other than the one the pilot is going to. Taxi clearance form: {taxi_form}. Never invent wind, altimeter, \
frequencies or traffic. If a value you would need is missing, say "unable" instead of guessing.
4. Stay inside your role ({role}). If the pilot needs another position, tell them whom to contact \
using a frequency from CONTEXT.
5. If the transmission is unclear or not meant for you, ask them to say again.
6. READBACKS are checked and answered by software, not by you: never say "readback correct" and never ask for a readback. Never add "say again" after giving an instruction; "say again" is only for transmissions you could not understand.
6b. A radio check gets "<callsign>, loud and clear." and nothing more.
6c. Answer only what the pilot asked. Never give taxi, takeoff or landing instructions the pilot did not request. \
A readback or an acknowledgement ("roger", "wilco") needs no new instruction.
7. Wind, altimeter/QNH and the runway in use appear in CONTEXT only when they are known. State only \
what is given there. If wind or altimeter is missing, do not state or invent it.
8. Traffic information is given from the pilot's point of view, using the "from the pilot" values in CONTEXT: \
clock position, distance, direction of flight and level (e.g. "traffic, two o'clock, three miles, northwest bound, \
one thousand feet below"). Never state an aircraft type, airline or intention that CONTEXT does not give.
9. If CONTEXT has a RUNWAY STATUS block, it decides who may take off or land. Follow it exactly.

AIRPORT
{airport_block}

LOCAL NOTES
{notes}
"""


def phraseology_for(airport: Airport) -> str:
    return "FAA" if airport.country == "US" else "ICAO"


def build_system_prompt(airport: Airport, facility: Facility) -> str:
    runways = "\n".join(_runway_line(r) for r in airport.runways) or "  (none on file)"
    airport_block = (
        f"{airport.icao} {airport.name}, elevation {airport.elevation_ft:.0f} ft, "
        f"{'towered' if airport.towered else 'non-towered'}\n"
        f"Runways:\n{runways}\n"
        f"Frequencies: {describe_frequencies(airport.frequencies)}"
    )
    notes = "\n".join(f"- {n}" for n in airport.notes) or "- none"
    faa = phraseology_for(airport) == "FAA"
    return SYSTEM_TEMPLATE.format(
        facility_name=callsign_for(airport, facility),
        freq=facility.freq.mhz,
        role=facility.role,
        phraseology=phraseology_for(airport),
        number_rule=(
            "digit by digit, 9 as 'niner'. Frequencies with '" + ("point" if faa else "decimal") + "' "
            "(121.9 = 'one two one " + ("point" if faa else "decimal") + " niner'), flight levels digit by digit "
            "('flight level two zero zero'), "
            + ("altimeter digit by digit ('altimeter two niner niner two')" if faa else "QNH digit by digit ('QNH one zero two zero')")
            + ", runways digit by digit ('runway one three')."
        ),
        taxi_form=(
            "'runway X, taxi via <TAXI ROUTE>' (without a route: 'runway X, taxi')"
            if faa
            else "'taxi to holding point runway X via <TAXI ROUTE>, QNH <QNH>' (without a route: 'taxi to holding "
            "point runway X, QNH <QNH>'; leave out QNH if it is not available)"
        ),
        airport_block=airport_block,
        notes=notes,
    )


def _runway_line(r) -> str:
    bits = [f"  {r.ident}"]
    if r.heading_deg is not None:
        bits.append(f"heading {r.heading_deg:.0f}")
    if r.length_ft:
        bits.append(f"{r.length_ft:.0f} ft")
    if r.pattern_alt_agl_ft and r.pattern_direction:
        bits.append(f"pattern {r.pattern_direction} traffic at {r.pattern_alt_agl_ft} ft AGL")
    else:
        bits.append("pattern: UNKNOWN")
    return ", ".join(bits)


def build_context(
    own: OwnState,
    traffic: list[Traffic],
    airport: Airport,
    spoken_callsign: str | None = None,
    role: str | None = None,
    first_contact: bool | None = None,
    preferred_runway: str | None = None,
) -> str:
    """Live state block, rebuilt every turn and prepended to the pilot's transmission.
    `preferred_runway`: the flight plan's departure runway (see runway.runway_in_use)."""
    d = distance_nm(own.lat, own.lon, airport.lat, airport.lon)
    brg = bearing_deg(airport.lat, airport.lon, own.lat, own.lon)
    lines = [
        "CONTEXT (live)",
        f"Pilot callsign: {own.callsign} ({own.aircraft_type}), spoken callsign: \"{spoken_callsign or own.callsign}\"",
        f"Transponder currently set to {own.squawk} (what the pilot has dialed, NOT an assigned code)",
        (
            "Pilot is on the ground"
            if own.on_ground
            else f"Pilot is airborne at {own.alt_msl_ft:.0f} ft MSL ({own.alt_agl_ft:.0f} ft AGL)"
        )
        + f", ground speed {own.gs_kt:.0f} kt, heading {own.heading_deg:.0f}",
        f"Position: {d:.1f} NM {compass_point(brg)} of the airport",
    ]
    if first_contact is not None:
        lines.append("This is your first contact with this pilot: say your station name once." if first_contact
                     else "Not first contact: do not say your station name.")
    lines += _weather_lines(own, airport, preferred_runway)
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt, preferred_runway)
    if role == "ground" and rwy is not None and own.on_ground:
        route = airport.taxi_routes.get(rwy.ident)
        lines.append(f"TAXI ROUTE to runway {rwy.ident} (from the charts, treat as fact): {route}" if route
                     else "No taxi route on file: give the taxi clearance without naming any taxiway.")
    lines.append("Nearby traffic (aircraft type unknown):")
    if not traffic:
        lines.append("  none reported")
    for t in sorted(traffic, key=lambda t: distance_nm(airport.lat, airport.lon, t.lat, t.lon))[:8]:
        td = distance_nm(airport.lat, airport.lon, t.lat, t.lon)
        tb = bearing_deg(airport.lat, airport.lon, t.lat, t.lon)
        state = "on ground" if t.on_ground else f"{t.alt_msl_ft:.0f} ft MSL, {compass_point(t.heading_deg)} bound"
        lines.append(f"  {t.callsign}: {td:.1f} NM {compass_point(tb)} of airport, {state}, {t.gs_kt:.0f} kt; "
                     f"from the pilot: {_relative(own, t)}")
    return "\n".join(lines)


def _relative(own: OwnState, t: Traffic) -> str:
    """Traffic as a controller tells it to this pilot: clock position, distance, level difference."""
    d = distance_nm(own.lat, own.lon, t.lat, t.lon)
    rel = (bearing_deg(own.lat, own.lon, t.lat, t.lon) - own.heading_deg) % 360
    clock = round(rel / 30) % 12 or 12
    out = f"{clock} o'clock, {d:.1f} NM"
    if not (own.on_ground and t.on_ground):
        dalt = t.alt_msl_ft - own.alt_msl_ft
        out += ", same level" if abs(dalt) < 300 else f", {abs(dalt):.0f} ft {'above' if dalt > 0 else 'below'}"
    return out


def _weather_lines(own: OwnState, airport: Airport, preferred_runway: str | None = None) -> list[str]:
    out: list[str] = []
    if own.wind_dir_deg is not None and own.wind_kt is not None:
        if own.wind_kt <= 3:
            out.append("Wind: calm")
        else:
            out.append(f"Wind: {own.wind_dir_deg:03.0f} degrees at {own.wind_kt:.0f} knots")
    else:  # say so explicitly: small models fill a silent gap with an invented value
        out.append('Wind: NOT AVAILABLE (if asked, reply "wind not available"; never state a wind)')
    if own.qnh_hpa is not None:
        if airport.country == "US":
            out.append(f"Altimeter: {own.qnh_hpa * 0.02953:.2f} inches")
        else:
            out.append(f"QNH: {own.qnh_hpa:.0f} hectopascals")
    else:
        out.append('Altimeter/QNH: NOT AVAILABLE (if asked, reply "altimeter not available"; never state one)')
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt, preferred_runway)
    if rwy is not None:
        out.append(f"Runway in use (computed from wind, treat as fact): {rwy.ident}")
    return out


def build_messages(
    system: str, context: str, history: list[tuple[str, str]], pilot_text: str, max_turns: int = 6
) -> list[dict]:
    """OpenAI-style messages. `history` holds (pilot, atc) pairs, oldest first."""
    msgs: list[dict] = [{"role": "system", "content": system}]
    for pilot, atc in history[-max_turns:]:
        msgs.append({"role": "user", "content": pilot})
        msgs.append({"role": "assistant", "content": atc})
    msgs.append({"role": "user", "content": f"{context}\n\nPILOT TRANSMISSION: {pilot_text}"})
    return msgs
