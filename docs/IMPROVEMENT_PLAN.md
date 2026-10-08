# Improvement plan (2026-10-07): ATIS/weather, ground ops, a launcher, and what SayIntentions does

STATUS 2026-10-08: blocks 1-3 of "Order of work" BUILT (fake sim + tests, 286 tests; 16 simulated hours of our
traffic at SABE and SARC with no runway conflict and nothing stuck): A1 A2 A3 A4 B1 B2 (block 1), I1 I2 I3 (block
2), B3 B4 B5 (block 3), plus Ground re-routing and head-on deconfliction for our traffic. Blocks 4-7 are in
docs/ROADMAP.md "NEXT". In-sim checks: docs/ROADMAP.md "WATCH IN THE SIM 2026-10-08".

Valen asked: crossings handed to Tower in reduced visibility; ATIS everyone follows, and at airports without one the
controller gives the weather (VATSIM style); a graphical launcher; every ATC feature of SayIntentions.AI we could
have. Plan only: work it in the order of "Order of work" at the end, each block ends with a flight in the sim.

## How the ATIS works today (answer to "when are they generated?")
- On demand, from `atis.build()`: when the user tunes the ATIS (COM1, or COM2 with receive on: the watcher builds it
  and loops it), on a first call to a position (letter check, and since 2026-10-07 "confirm information X" when the
  call has no letter), and when one of our aircraft makes its first call (it says the current letter).
- One `AtisState` per session: the letter of each airport moves on (A -> B ...) when what the ATIS says changes:
  the METAR observation time (else the hour), the runway in use, the QNH rounded. The start letter is fixed per
  airport and day. Everyone (the user, our traffic) reads the same state, so they agree.
- Weather: live METAR (aviationweather.gov, cached) if there is one, else the sim's weather at the user's aircraft
  when near the field. `ATC_METAR=off` for preset weather. Gap: the letter only moves when someone builds it, and
  nothing tells pilots already on frequency that it changed.
- Airports with no ATIS frequency (SARC has only Tower 118.3): no ATIS. Controllers give QNH in the taxi clearance
  and the wind with takeoff / landing clearances, nothing else (no visibility, clouds, temperature).

## A. ATIS and weather
A1. ATIS on a clock: the watcher rebuilds the ATIS of the airports in play (origin, destination, nearest) once a
    minute, so the letter changes when the METAR does (hourly + specials), not when somebody asks.
A2. "All stations" broadcast on a change of QNH (>= 1 hPa) or runway, on Tower / Approach / Ground of that airport
    (ICAO: "all stations, Aeroparque Tower, information Charlie now current, QNH one zero zero nine"). Our aircraft
    and the user's next calls then use the new letter.
A3. No ATIS (VATSIM style), code-owned, first contact only:
    - departing: Delivery/Ground (Tower at SARC) adds "runway two zero in use, wind zero four zero degrees eight knots,
      visibility one zero kilometres, QNH one zero one three, temperature two four" to its first reply;
    - arriving: Approach (else Tower) on check-in: "expect ILS approach runway two zero, wind ..., QNH ...", plus
      visibility / ceiling only when below VMC (vis < 5 km, ceiling < 1500 ft) and significant weather (TS, RA, FG).
    Our aircraft at such airports don't say a letter (they say "with the numbers" in the US: FAA phrase).
A4. Weather source by itself: compare the sim's QNH/wind at the field with the METAR; if they disagree beyond
    tolerance the sim is on preset weather -> use the sim's (no more `ATC_METAR=off` by hand). Printed once.
A5. Low-visibility procedures: vis < 550 m or ceiling < 200 ft -> ATIS "low visibility procedures in operation",
    approach "cleared ILS ... CAT II/III", larger arrival spacing for our traffic, crossings only by Tower.

## B. Ground and runway
B1. Runway crossings by Tower in reduced visibility (Valen): VMC -> Ground clears the crossing (today); visibility
    < 5 km or ceiling < 1500 ft (or LVP) -> Ground: "hold short of runway one three, contact Tower one one eight
    decimal eight five"; Tower clears it, then "contact Ground" after. Airport YAML `crossings_by: tower|ground|auto`
    (default auto). Same for our aircraft (their crossing exchange moves to Tower).
B2. Backtrack where there is no parallel taxiway (SARC 02/20): "backtrack runway two zero, line up and wait" /
    "...cleared for takeoff"; for arrivals "backtrack, vacate via ..." (vacate backtrack exists). Detect from the
    taxi map: the holding point is not at the runway end -> the line-up path runs back along the runway. User + ours.
B3. One runway controller for everybody (own-traffic phase 3): the user joins the same departure queue as ours
    (first at the holding point goes first; "number two for departure"); conditional line-up behind our landing
    aircraft by type ("behind the landing Embraer one ninety"); arrivals sequenced together ("number three,
    follow the Boeing seven thirty-seven on six mile final"); traffic information naming our aircraft.
B4. Wake turbulence: "caution wake turbulence" (light/medium behind heavy), 2 / 3 min departure spacing behind
    heavies, "cleared for immediate takeoff" when an arrival is close.
B5. Delays at busy times: "expect push in five minutes, number three for push" when the apron is busy; "expect
    departure in ..." with a long queue (computed from our traffic queue).
B6. Intersection departures on request ("request departure from Delta": approved if the remaining runway is long
    enough for the type).

## C. Approach and arrival
C1. Visual approach: "report field in sight" -> "cleared visual approach runway one three" (VFR and IFR).
C2. ATC-initiated holding when the runway is blocked or the sequence is full (holding.py has the phrasing).
C3. Side-step to the parallel at KSFO / JFK ("cleared ILS two eight left, side-step two eight right").
C4. Minimum vectoring altitude from the terrain: vectors and descents never below the MSA of the sector (Navigraph
    MSA records) or a terrain sample (navdb), "climb immediately" if the pilot is below it.
C5. Missed approach handling: the published missed approach or "climb straight ahead, three thousand, contact
    Approach" (monitor has the go-around detection), then vectors for another approach.

## D. En route
D1. Weather deviations: "request deviation twenty degrees left of course" -> approved, "report back on course".
D2. Ride reports / PIREPs by code from the sim's winds aloft and the METARs ("moderate chop reported at FL240").
D3. Traffic information about our own departures/arrivals climbing out / descending on the user's route.

## E. VFR
E1. Class B/C transitions (US): "cleared through Class Bravo, maintain VFR at or below three thousand five hundred".
E2. Argentina CTR entry/exit with reporting points: needs the VAC points by hand in the YAML (open question).
E3. Untowered fields: our GA traffic makes CTAF self-announcements; user's calls on CTAF get no ATC answer but our
    traffic answers like pilots ("traffic in sight").

## F. Emergencies
F1. Mayday / pan pan follow-ups: "say souls on board and fuel remaining", runway of choice, emergency services
    alerted, our traffic held or sent around (monitor.py has the first reply).
F2. 7600 (radio failure): handled as no-radio arrival (light-gun phrases in text), our traffic held clear.

## G. Controller behaviour
G1. Readback mode: strict (today) or relaxed (learning: only safety items corrected), a launcher setting.
G2. Blocked transmissions: the user talking over chatter -> "blocked, say again" (bus knows when chatter plays).
G3. Busy controller: "standby" then the answer a few seconds later when the frequency is busy (beyond Delivery).
G4. Closures by hand: airport YAML `closed: [taxiway E, runway 13/31 until ...]` respected by routes and the
    runway choice (NOTAM-lite; real NOTAM feeds only exist for the US FAA, low priority).

## H. Own traffic (after phase 3 = B3)
H1. Turnarounds (an arrival becomes a departure from the same stand after 45-90 min).
H2. Arrivals from the STAR / downwind instead of a straight 12 NM final; departures following the SID's first
    legs (traffic information then makes sense en route).
H3. GA circuits at small fields (SARC), CTAF calls (E3).

## I. Launcher (graphical start-up)
I1. `atc_config.toml` (in the user's AppData) holding every option; `python -m atc` reads it, command-line
    options override it (nothing breaks for the old way).
I2. A launcher window (tkinter: comes with Python, nothing to install; one file `src/atc/gui.py`, started by a
    `vATC.bat` / `pythonw -m atc.gui` shortcut):
    - Flight: sim / fake sim, airport (auto), SimBrief user + "fetch plan" button (shows route, SID, cruise),
      callsign / telephony (auto from the plan).
    - Radio: microphone and headset dropdowns (sounddevice list), PTT key, "press your yoke button" detection
      (probe_ptt logic), voice + "test voice" button, radio filter on/off.
    - ATC: LLM provider order with a key-present check per provider, live METAR vs sim weather (or auto, A4),
      readback mode (G1), ATIS question on/off.
    - Traffic: own traffic on/off, amount slider (factor), max aircraft, prefer FS Traffic / FSLTL, tug on/off.
    - Start / Stop; the log in the window.
I3. A live panel while flying (same window, second tab): the airport's frequencies with the active one marked,
    the current ATIS letter + text, the transcript (pilot / ATC / chatter), our traffic list with state, buttons
    for /owndep /ownarr, "say again" and a text box to type a call (no microphone needed).
I4. Later, optional: an in-sim toolbar panel (MSFS InGamePanel, like FS Traffic's) talking to the app over a local
    websocket. Much bigger; only if the window isn't enough.

## J. Voices and language (already planned)
J1. Voice comparison tool (Piper / Kokoro / Edge) -> pick (docs/OWN_TRAFFIC_PLAN.md "Voices").
J2. Spanish ATC: Spanish STT model, phraseology tables, daniela / claude voices (downloaded).

## SayIntentions.AI ATC features vs ours
Sources: sayintentions.ai/atc, the FSExpo 2026 announcements, simflight's roadmap article (2026-06).
| SayIntentions | Here | Plan |
|---|---|---|
| Unscripted IFR/VFR ATC, clearance to arrival | yes (code-owned fixed forms + LLM free form) | - |
| Real taxi routing, hold short / crossings matched to the active runway, progressive taxi | yes | B1 Tower crossings |
| Conditional clearances, give way, line up and wait | yes | B3 behind our traffic by type |
| No-delay departures with wake turbulence cautions | no | B4 |
| Readback enforcement, strict / flexible mode | strict only | G1 |
| Click-to-talk (phraseology shown before sending) | no | I3 suggested calls in the live panel |
| Sequencing with position in line, speed restrictions, holding | partly (speed control, holding on request) | B3, C2 |
| Charted visual approaches (River Visual...), side-step approaches | no | C1 plain visual, C3 side-step; named visuals later |
| Terrain-aware vectoring | no | C4 |
| Dynamic rerouting / direct-to evaluated live | direct-to yes | D1 deviations |
| Live TFRs, NOTAM-aware (closed runways/taxiways) | no | G4 by hand; TFR US only: skip |
| PIREPs | no | D2 by code |
| Emergencies with souls/fuel, priority | first reply only | F1 |
| Radio chatter, a different controller per facility with accents | yes | J1 better voices |
| Schedule-based traffic that ATC controls ("Living World", "True IFR context") | yes (own traffic, phase 1-2) | B3, H |
| ATIS changes reflected live | partly | A1, A2 |
| 15 languages | English | J2 Spanish |
| VATSIM handoffs, multiplayer sequencing | no | skip (Valen: VATSIM or AI ATC, not both) |
| CPDLC / ACARS, copilot, cabin crew, tour guides, PlateBrief, Backtrack, Spotting, FlowPro | - | not ATC: skip (FlowPro ~ I4) |

## Order of work (each line about one session, then a flight)
1. Weather and ATIS: A1, A2, A3, A4, B1 (Tower crossings), B2 (backtrack: SARC needs it).
2. Launcher: I1, I2, I3 (makes every later test flight quicker to start).
3. Shared runway controller: B3, B4, B5 (own-traffic phase 3).
4. Approach: C1, C2, C4, C5; D1.
5. Controller behaviour + emergencies: G1, G2, G3, F1, A5 (LVP).
6. Own traffic extras: H1, H2, H3 / E3; then C3, E1, D2, D3, G4, B6.
7. Voices J1; Spanish J2 when Valen wants it.
