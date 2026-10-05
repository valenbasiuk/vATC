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
2. Use standard {phraseology} phraseology. Be brief: one to three short sentences.
3. Use ONLY the runways, frequencies and traffic listed under CONTEXT. Never invent runways, \
frequencies, taxiways, wind, altimeter or traffic that are not given. If a value you would need is \
missing, say "say again" or "unable" instead of guessing.
4. Stay inside your role ({role}). If the pilot needs another position, tell them whom to contact \
using a frequency from CONTEXT.
5. If the transmission is unclear or not meant for you, ask them to say again.
6. Read back the key items of every clearance as a controller would expect, and demand a readback \
when the pilot omits one for a runway, hold-short, takeoff or landing clearance.
7. Wind, altimeter/QNH and the runway in use appear in CONTEXT only when they are known. State only \
what is given there. If wind or altimeter is missing, do not state or invent it.

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
    return SYSTEM_TEMPLATE.format(
        facility_name=callsign_for(airport, facility),
        freq=facility.freq.mhz,
        role=facility.role,
        phraseology=phraseology_for(airport),
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


def build_context(own: OwnState, traffic: list[Traffic], airport: Airport) -> str:
    """Live state block, rebuilt every turn and prepended to the pilot's transmission."""
    d = distance_nm(own.lat, own.lon, airport.lat, airport.lon)
    brg = bearing_deg(airport.lat, airport.lon, own.lat, own.lon)
    lines = [
        "CONTEXT (live)",
        f"Pilot callsign: {own.callsign} ({own.aircraft_type}), squawk {own.squawk}",
        (
            "Pilot is on the ground"
            if own.on_ground
            else f"Pilot is airborne at {own.alt_msl_ft:.0f} ft MSL ({own.alt_agl_ft:.0f} ft AGL)"
        )
        + f", ground speed {own.gs_kt:.0f} kt, heading {own.heading_deg:.0f}",
        f"Position: {d:.1f} NM {compass_point(brg)} of the airport",
    ]
    lines += _weather_lines(own, airport)
    lines.append("Nearby traffic:")
    if not traffic:
        lines.append("  none reported")
    for t in sorted(traffic, key=lambda t: distance_nm(airport.lat, airport.lon, t.lat, t.lon))[:8]:
        td = distance_nm(airport.lat, airport.lon, t.lat, t.lon)
        tb = bearing_deg(airport.lat, airport.lon, t.lat, t.lon)
        state = "on ground" if t.on_ground else f"{t.alt_msl_ft:.0f} ft MSL"
        lines.append(f"  {t.callsign}: {td:.1f} NM {compass_point(tb)} of airport, {state}, {t.gs_kt:.0f} kt")
    return "\n".join(lines)


def _weather_lines(own: OwnState, airport: Airport) -> list[str]:
    out: list[str] = []
    if own.wind_dir_deg is not None and own.wind_kt is not None:
        if own.wind_kt <= 3:
            out.append("Wind: calm")
        else:
            out.append(f"Wind: {own.wind_dir_deg:03.0f} degrees at {own.wind_kt:.0f} knots")
    if own.qnh_hpa is not None:
        if airport.country == "US":
            out.append(f"Altimeter: {own.qnh_hpa * 0.02953:.2f} inches")
        else:
            out.append(f"QNH: {own.qnh_hpa:.0f} hectopascals")
    rwy = runway_in_use(airport, own.wind_dir_deg, own.wind_kt)
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
