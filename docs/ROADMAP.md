# Roadmap

Ordered. Each item has a "done when" so it can be checked. Status as of 2026-10-08.
Legend: DONE (verified), WORKING (runs on Valen's PC, loose ends listed), TODO.
The plan for the next session is in docs/NEXT_SESSION.md; the improvement plan (with the SayIntentions.AI
comparison) is docs/IMPROVEMENT_PLAN.md.

## NEXT: improvement plan blocks 4-7 (Valen, 2026-10-08: "leave the next four in a roadmap")
Blocks 1-3 (ATIS/weather, the launcher, the shared runway controller) are built; see "WATCH IN THE SIM 2026-10-08"
below. The rest, in this order (codes as in docs/IMPROVEMENT_PLAN.md):
4. Approach (TODO)
   - C1 visual approach: "report field in sight" -> "cleared visual approach runway one three" (VFR and IFR).
     Done when: a VFR or IFR arrival at SABE in VMC gets it without the LLM.
   - C2 ATC-initiated holding when the runway is blocked or the sequence is full (holding.py has the phrasing).
     Done when: with our traffic queued, an IFR arrival is told "hold at ... as published, expect further clearance".
   - C4 minimum vectoring altitude: vectors / descents never below the sector MSA (Navigraph MSA records) or a
     terrain sample; "climb immediately" below it. Done when: no vector below MSA in a SARC / SAMR test.
   - C5 missed approach: the published one or "climb straight ahead, three thousand, contact Approach", then vectors
     for another approach. Done when: a go-around at SABE ends in a second approach without the LLM.
   - D1 weather deviations: "request deviation twenty degrees left" -> approved, "report back on course".
5. Controller behaviour and emergencies (TODO)
   - G1 readback mode strict / relaxed (a launcher setting). G2 "blocked, say again" when the user talks over
     chatter. G3 a busy controller says "standby" and answers seconds later.
   - F1 mayday / pan pan follow-ups (souls on board, fuel, runway of choice, our traffic held or sent around).
   - A5 low-visibility procedures (vis < 550 m or ceiling < 200 ft): ATIS "LVP in operation", CAT II/III phrasing,
     wider spacing of our arrivals, crossings by Tower only. (atis.Conditions.low_visibility exists.)
6. Own traffic extras (TODO)
   - H1 turnarounds (an arrival leaves again from its stand after 45-90 min); H2 arrivals from the STAR / downwind
     and departures on the SID's first legs; H3 GA circuits and CTAF calls at small fields (E3).
   - C3 side-step (KSFO/JFK), E1 Class B/C transitions (US), D2 ride reports by code, D3 traffic information on our
     departures/arrivals en route, G4 closures by hand in the airport YAML, B6 Ground taxiing to an intersection
     on request (Tower's intersection departure is built).
7. Voices and language (TODO)
   - J1 voice comparison tool (Piper high voices downloaded / Kokoro / Edge neural) -> pick.
   - J2 Spanish ATC (Spanish speech model, phraseology tables, es_AR daniela voice: downloaded).

## WATCH IN THE SIM 2026-10-08, second round (Valen's SABE flight; fake sim + tests; 298 tests)
- Ground -> Tower handoff now needs Ground's taxi clearance and not being at a stand (SABE gates 27-29 lie inside
  the runway-13 handoff box: parked there, "contact Tower" came twice unasked).
- AI traffic reader: a SimConnect exception no longer turns it off for the flight; it backs off 5/15/30/60 s and
  opens a new connection after 3 failures ("[traffic: AI read failed (SimConnect exception N (NAME)), retrying in
  ...]"). Our own traffic stays listed meanwhile. Tell me the exception NAME if it shows up.
- Warm start (own traffic): at the start, more likely the busier the field, one of ours already on a 3.5-8 NM final
  and one taxiing out or at the holding point ("[own traffic: X B738 at the holding point -> runway 13 ...]").
- Voices: nobody heard in the last 15 min shares a voice; the Spanish pool (4 people) is stretched with pitch
  variants (0.92 / 1.08) before going to the generic pool. Listen to voice_samples/pitch_variants.wav: if the
  variants sound robotic, narrow voices.PITCHES.
- Lights on our aircraft: battery on + light simvars written on the object + the *_SET events (ATC_OWN_LIGHTS=
  events|data|both). Run `python tools/probe_lights.py` (dusk/night) and tell me which step lit them.
- Intersection departure: "request intersection departure" / "from present position" / "no backtrack" -> "runway
  one three from intersection Bravo, one thousand five hundred metres available, cleared for takeoff" (taxiway from
  the taxi map). At a mid-runway holding point with an arrival inside 8 NM (backtrack blocked) Tower asks "advise
  able to depart from runway two zero, intersection Bravo, ... metres available" when the runway left is enough for
  the type (L 600 m, turboprop 1100, M 1700, H 2800); "affirm" -> takeoff, "negative" -> backtrack later.
  Not yet: Ground taxiing you to an intersection on request.
- FS Traffic's own AI (callsign labels like "5231/B738/BONDI", ARG1780) is not ours: it doesn't hear this ATC, can
  enter the runway unasked, and its go-around ended on the ground. With --own-traffic, FS Traffic's injection
  must be OFF (keep the package: its models and schedules are what we use).

## WATCH IN THE SIM 2026-10-08 (blocks 1-3, fake sim + tests only; 286 tests)
- ATIS on a clock: the letter changes with the METAR; a QNH or runway change is broadcast "all stations" on the
  position you are tuned to (main._atis_clock, once a minute). Too chatty? wrong station name?
- Sim's own weather detected (atis.sim_weather: QNH off the METAR by > 3 hPa or wind speed by > 15 kt): with preset
  weather the ATIS drops the METAR's clouds / visibility and uses the sim's visibility (AMBIENT_VISIBILITY: VERIFY
  it comes back in metres). A "[weather at SABE: ...]" line says which.
- No ATIS (SARC): the first reply adds "runway two zero in use, wind ..., temperature ..., QNH ..." (departing) or
  "expect ILS approach runway two zero, wind ..., QNH ..." (arriving). Remember to read the QNH back.
- "Confirm information X" when the first call has no letter; "affirm" ends it, "negative" gets the current one.
- Runway crossings by Tower when visibility < 5 km / ceiling < 1500 ft (YAML `crossings_by: tower|ground|auto`).
- Backtrack at SARC: "backtrack runway two zero, line up and wait", the takeoff clearance once lined up at the end
  (flow.lined_up_at_end: on the runway, < 0.15 NM from the threshold, aligned, < 3 kt). Ours backtrack and turn
  round 60 m in; does the turn look right on SARC's 45 m runway?
- Shared runway controller: "number two for departure" behind ours (whoever reported ready first), 1 / 2 / 3 min
  departure intervals (wake), "caution wake turbulence", "cleared for immediate takeoff" with an arrival at 3-5 NM;
  your push held ("expect push and start in two minutes, number three, I'll call you") with 4+ of ours busy.
- Our traffic: Tower sends a cleared one of ours around if the runway gets occupied inside 2.5 NM (you lining up);
  Ground re-routes ours round one that is stuck ("change of routing, taxi via ..."); a rare last resort lets one
  pass through another (printed "[own traffic: X passes Y (deadlock)]": tell me if you see it).
- Launcher: `vATC.bat` (pythonw -m atc.gui). Device lists, yoke-button detection, voice test, SimBrief fetch, the
  live panel (status file in %TEMP%\\vatc_status.json), typed calls while using push-to-talk (--stdin).

## WATCH IN THE SIM: built 2026-10-06 against the fake sim only (possible in-sim issues)
Nothing below has been flown in MSFS. Each line: what might go wrong, what to look for, where the knob is.
When Valen reports one, fix it and move the line to "verified" (or delete it).
- **VFR circuit** (`pattern.py`): position reports depend on STT hearing "downwind"/"base"/"final"; a misheard call
  goes to the model. Straight-in only when within 1.5 NM of the centerline and pointing along it
  (`STRAIGHT_IN_NM`); a wide downwind gets "join downwind" instead. The watcher clears a circuit aircraft on
  2.5 NM final if it didn't call (`AUTO_CLEAR_NM`): could fire on a long final the pilot hasn't reported yet.
  Touch and go / full stop rely on `flow.track` on-ground transitions (a bounce can look like a touch and go; a
  landing within 120 s of takeoff isn't counted). "report left downwind" after a touch and go fires above 300 ft AGL.
  SABE/SARC pattern side and height are ASSUMED (left, 1000 ft) in the YAMLs.
- **Zone transit** (`pattern._TRANSIT`): only "transit / cross the zone / overfly / through the zone" wordings
  are recognised; other wordings with "N miles north" become a join.
- **VFR departure without a plan**: a turn-out request (or "VFR") means "frequency change approved" at 8 NM
  (`LEAVE_ZONE_NM`), no Departure; a plain "ready for departure" without a plan still goes to Departure.
- **Area control / FIRs**: Argentine hand files are chosen by the NEAREST REFERENCE POINT, not the real boundary:
  the Ezeiza -> Resistencia/Cordoba handoff may come early or late. Navdata FIRs (`navdb.fir_at`) are AIRAC 1801:
  frequencies may not match MSFS. Center -> Center handoffs fire once per FIR+frequency.
- **Tower -> Control at fields without Departure** (SARC): at 2000 ft AGL or 5 NM (`TOWER_TO_CONTROL_AGL_FT`). With
  the sim db loaded SARC gets APP 118.1/118.7, so this may not trigger there.
- **FAA initial altitude** ("maintain five thousand", no SID): the level-bust monitor watches it until Departure
  climbs you; "check altitude" if you climb through 5300 ft before the Departure check-in. SimBrief often has no
  SID for US radar-vector departures: then this is what you get.
- **Runway crossings** (`taxi.crossings`): need runway threshold positions (from the YAML or the sim db) and a
  taxi map; the runway ends in the sim db are pavement ends, not displaced thresholds. "Holding short" is only
  answered within 0.25 NM of the crossing point and below 5 kt (`CROSSING_NEAR_NM`). A route along a runway edge
  or past a runway end (within 0.01 NM) could give a false "hold short".
- **Runway configs** (KSFO `runway_configs`, from general knowledge): Ground may send you to 1L while SimBrief
  planned 28L (the SID may not fit). Any new call site must pass `use=` or use `runway.session_runway`.
- **Runway requests** (`flow.runway_request`): approved up to 10 kt tailwind (`REQUEST_MAX_TAILWIND_KT`), no
  traffic check; the approval sticks for that airport+direction for the whole session. Approach requests reset the
  vectors. "Runway" must be heard by STT ("request runway one three" / "request ILS one three").
- **Approach/Departure chatter for AI** (`tracker._radar_event`): departure check-in at 2000 ft AGL, approach call
  7-15 NM out heading roughly at the field (within 100 degrees), "contact tower" inside 10 NM. Real AI on STAR
  downwind legs may be missed or called late; only the first APP/DEP frequency in the YAML carries this chatter.
- **Conditional line-up / automatic takeoff clearance** (`sequence.wait_for_takeoff`, `flow.takeoff_when_clear`):
  the takeoff clearance comes by itself as soon as nobody is on the runway or inside 3 NM final; if the landing AI
  rolls out slowly it may come late, if AI positions jump it may come early.
- **Speed control** (`enroute._speed_text`): `AIRSPEED_INDICATED` units unverified (falls back to ground speed,
  which is off by the wind). Speeds: 210 on vectors, 180 on the intercept, 160 with traffic < 6 NM ahead.
- **Ground conflicts** (`ground.py`) — "give way" WORKED in the real sim (Valen, 2026-10-07). Kept for the record:
  straight-line prediction for 30 s from the actual movement, called after 2
  ticks in a row (`PERSIST_TICKS`), within 65 m (`CONFLICT_NM`). Real AI turns a lot on taxiways: false
  "give way" calls are the main risk; also missed ones when an AI turns into you. Only on Ground.
- **Progressive taxi** (`taxi.turn_calls`): the turn is said ~150 m before it (`TURN_CALL_NM`); off the computed
  route the calls are meaningless (only said near a turn point, so mostly silent). Turn side from the path ±3
  nodes: dense or odd graph geometry can give the wrong side.
- **Approach names** (`navdb.approach_type`): plain, then Z, Y, X: the controller's real choice may differ. GPS
  rows with suffix A/D in the sim db are treated as STARs/SIDs (an airport whose only RNAV approach is coded that
  way would lose it).
- **STAR descent**: SimBrief often leaves `star_ident` empty (Valen's SABE-SAAR plan has none) -> no "descend via".
  FAA "descend via" turns the level-bust monitor off until the next assigned altitude.
- **VFR flight following** (`following.py`): ends only near a LOADED airport (8 NM, below 3000 ft AGL); flying to an
  airport that isn't loaded, nobody terminates the service. The squawk must be set exactly.
- **KSFO runway thresholds** now come from the sim db (`navdb.enrich`): "on final"/"on the runway" at KSFO changed
  because of it (was all on the reference point before).
- **Readback stemming** (`readback._stem` drops a final "e"): more replies may count as acknowledgements.
- **"Say again"** (main.py `_SAY_AGAIN`): repeats the last transmission of the position you're on, word for word;
  if the last thing on that frequency was a controller call made by the watcher, that is what gets repeated.
- **Holding** (`holding.py`): published holds from the Navigraph db (AIRAC 1801) within 50 NM of the fix, else
  "inbound track <current bearing to the fix>, right turns"; FAA hold direction = opposite the inbound course
  (8-point compass, may not match the chart's wording); level = the cleared level or the current altitude rounded
  to 1000 ft; EFC = sim zulu + 15 min. No descent/vectors while holding; released on "request approach" / "ready to
  leave the hold" or at the EFC. The pilot flies the hold; nothing checks it.
- **Info answers** (`info.py`): "radio check" -> "read you five" (FAA "loud and clear"), "time check", "say QNH /
  wind" answered by code only when the call asks nothing else; the wind is the surface wind main.py keeps (when
  high or far it is the airport's, not the wind at the aircraft).
- **Line up and wait behind a departure** (`sequence.wait_for_takeoff`): an aircraft on the runway faster than
  30 kt and within 20 degrees of the runway heading counts as rolling; a slow AI backtracking the same way could be
  mistaken for it. The takeoff clearance follows when it is above 200 ft or off the runway.
- **AI traffic cache** (`SimConnectSource._ai_records`): one SimConnect AI request per 0.8 s, at least 30 NM wide;
  if the sim struggles with a big AI list at a busy airport, lower `AI_MIN_REACH_M`.
- **STT wording**: "take off", "down wind", "cross wind", "up wind" are joined before any check
  (`readback._normalize`); the STT prompt lists this session's phrases (`audio/stt.py`): watch whether a longer
  prompt changes recognition speed or accuracy.
- **Own navigation** (`enroute.own_nav_request`): "request own navigation / full procedure / ... via DOKMU" stops
  the vectors; the approach clearance comes inside 25 NM (`OWN_NAV_CLEAR_NM`) once the descent was given. The fix
  after "via" is taken as heard (not checked against the approach's transitions); "request vectors" goes back.
- **Whole-flight test** (tests/test_whole_flight.py): SABE -> SAAR in the fake sim with zero LLM calls; if a real
  flight says something extra, add that step to this test.
- **VFR departure to a compass direction** ("departure to the north", "northbound departure"): turn side from
  the runway's magnetic heading (within 30 degrees: straight out); marks the flight as a VFR departure (no
  Departure handoff).
- **Voices**: `voices/blacklist.txt` keys must match `tools/voice_samples.py` names exactly.

## A. Make it real on Valen's PC (Phases 0-1)
1. **Environment.** DONE: Windows venv, `pip install -e .[dev,sim,audio]`, 49 tests pass.
2. **Own telemetry from MSFS 2024.** WORKING: squawk encoding fixed (commit 2300896). Loose ends: the `VERIFY` notes in `sim/simconnect_source.py` (heading radians guard, wind/pressure units) were never formally ticked off; check them with `tools/probe_own.py` if a value looks wrong.
3. **AI traffic.** DONE, verified on Valen's PC: traffic around the airport works fine.
4. **Real airport data.** DONE, verified on Valen's PC: SARC and SABE generate perfectly; SAAR generated too. Hand fields added: `spoken_name` (SABE "Aeroparque", SAAR "Rosario"). `taxi_routes` is empty everywhere (see 9b).

## B. Voice (Phase 3)
5. **Piper.** WORKING: voice chosen, `voices/en_US-libritts-high.onnx` (commit ff34249).
5b. **Accents / one voice per person** DONE in code (2026-10-06, `voices.py`, `tools/download_voices.py`, already
    downloaded on Valen's PC): `en_US-l2arctic-medium` (24 non-native speakers: Spanish, Mandarin, Hindi, Korean,
    Arabic, Vietnamese) + `en_GB-vctk-medium` (109 speakers tagged English/Scottish/Irish/Welsh/American/Canadian/
    Australian/NZ/South African/Indian, from VCTK speaker-info) + libritts as the generic pool. AI pilot voice from
    its airline's country (OpenFlights airlines.dat) or registration prefix; each controller position (ICAO+role,
    also the ATIS) its own voice from the airport's country (Argentine controllers: Spanish accent); speaking rate
    varies per person, controllers a bit faster; per-station hiss + squelch tail (`radio_fx.squelch_tail`). Models
    preload in a thread at startup. Synthesis checked (0.1-0.2 s per sentence once loaded); NOT heard by Valen yet.
    `tools/voice_samples.py` writes one WAV per voice (voice_samples/<accent>/<key>.wav); `voices/blacklist.txt`
    drops a voice ("en_US-l2arctic-medium#3") or a whole accent ("accent:Spanish").
6. **STT.** PARTLY CHECKED (2026-10-05, Piper -> radio filter -> faster-whisper small.en round trip, no mic): ~2.6 s per call on CPU (over budget with LLM + TTS; try base.en), digits fine, names bad ("Martin Air", "Air park", "Atovil for Bravo"). Fixed in code: STT hint with station/telephony/SID/destination, split telephony joined, SID digit soundalikes, any first call on Delivery = clearance request. Still TODO / not reported yet: `FasterWhisperSTT` on CPU, `small.en` vs `base.en`. Done when a spoken call becomes correct text in about 1 s. Watch callsign and number accuracy: the clearance readback check is done by code on the transcript, so STT errors become "negative, I say again".
6b. STT 2026-10-06: `without_timestamps`, no conditioning on earlier text, temperature 0 (faster); the hint now
    also lists the sim's station names ("NorCal Departure", "Oakland Center").
7. **PTT + radio filter.** Keyboard key (one global hook, works with MSFS focused) and now joystick/yoke buttons
   (WinMM, `--ptt-joy`, `tools/probe_ptt.py`: on Valen's PC two controllers, device 2 has buttons 19/32 latched);
   `--mic` / `--audio-out` device selection. Still TODO / not reported yet: `--ptt` exists; check key handling, squelch click, filter. Done when you can say a call and hear a radio-sounding reply.

## C. Make the ATC good (Phase 4+), the part that decides if it's worth using
8. **Weather.** DONE in code: wind/QNH from the sim, stated only when known. METAR DONE (2026-10-06,
   `weather.py`): aviationweather.gov json (free, no key, works on Valen's PC), cached 10 min, fetched in a
   background thread (never blocks the radio), prefetched for the plan's airports; used for the ATIS (clouds,
   visibility, temperature, dew point, present weather) and as the destination's surface wind before a low reading
   exists (runway choice on arrival). `ATC_METAR=off` for preset weather. Winds said magnetic, rounded to 10 degrees.
9. **IFR departure at the origin.** DONE (2026-10-05), all decided by code, model not involved:
   - SimBrief plan via `--simbrief simbrief_last.json` (`flightplan.py`; `tools/probe_simbrief.py` fetches it).
   - Telephony learned from the pilot's first call ("Martinair 4133" -> "Martinair four one three three"), or `--telephony`.
   - Clearance (`clearance.py`): limit, SID + transition, flight planned route, climb via SID / expect filed level 10 min after departure, departure frequency (DEP, else APP), squawk (stable per callsign).
   - Asking the wrong position -> "contact Delivery <freq>". Optional "standby" with a callback 10-25 s later (`--standby`, default 0.3).
   - Readback checked item by item; wrong items -> "negative, I say again, <items>"; after a correction only those items are needed. Correct -> "readback correct, when ready for push and start contact Ground <freq>".
   - Push/start on Ground: approved, or "no clearance received yet, contact Delivery" if not read back.
   - Silence on correct readbacks and acknowledgements ("roger", "wilco", repeating the instruction). Wrong taxi readbacks -> code repeats the instruction ("negative, I say again, ...").
   - ICAO pronunciation done in code (`phrase.py`): digits, niner, "decimal", flight levels, SID names.
   - Audit fixes (2026-10-05): readback "flight level 200, 120.6" was rejected (numbers merged across the comma);
     Tower stayed silent on a first call that repeated Ground's "holding point runway 31" (readbacks now only count on
     the position that gave the instruction); calm wind sent the 31 SID to runway 13 (the plan's runway is kept up to
     5 kt tailwind); calls from another airline flight were answered as ours ("say again your callsign" now);
     registrations are spelled ("Lima Victor Alfa Bravo Charlie"); winds aloft no longer pick the runway; Departure/
     Approach are told when the squawk is wrong.
9a. **Airport data from the sim** DONE (2026-10-05, `navdb.py`): Little Navmap's MSFS 2024 scenery database
    (%APPDATA%/ABarthel/little_navmap_db/little_navmap_msfs24.sqlite, read-only; ATC_LNM_DB to override) gives the
    taxi network (named segments, holding points, stands: Rosario has only A and B, OSM's G was wrong), missing
    frequencies (Rosario Ground 121.85), magnetic variation, approach types per runway (Rosario 20 ILS, 02 RNAV) and
    waypoint positions (directs off the route). Added at load time; the YAMLs are never modified and hand values win.
    Sources checked: ChartFox needs a requested API token and serves AIP PDFs (no geometry); MSFS's in-sim LIDO charts
    have no API; the official AIP (ais.anac.gob.ar) is free (SABE AD 2.18 confirms TWR 118.85, GND 121.9, APP 120.6,
    CLR 129.3, ATIS 127.6; area control call sign "Ezeiza Control"). Ezeiza Control: 135.5 (Valen; VATSIM Argentina
    manual v1.2.1: SAEF_N_CTR 135.500 Centro Norte, SAEF_CTR 134.500 combined, SAEF_S_CTR 125.200 Sur) in
    airspace/SAEF.yaml; Departure -> Control -> Rosario handoffs now run. Little Navmap's Navigraph db is AIRAC 1801
    (2018), too old to trust.
9b. **Taxi routes.** WORKING (2026-10-05, `taxi.py`, code-owned): graph from OpenStreetMap (`tools/fetch_osm_taxi.py ICAO` -> `airports/osm/ICAO.json`; done for SABE, SAAR, SARC), Dijkstra with a penalty per taxiway change, goal = runway holding point at the departure end (else the taxiway node next to the threshold). Ground: "taxi to holding point runway 31 via Kilo, Alfa, QNH ..."; after landing "taxi to stand 12 via ..." (requested stand, else a free one). Taxiway names are read-back checked. Hand `taxi_routes` in the YAML always win. Coverage: SABE good, checked against Valen's LIDO chart 2026-10-05 (A parallel ~107 m NE of the centerline; B C D E F H I J K L M as on the chart; OSM's stand lead-in "1" is filtered, designators must start with a letter; apron -> 31 "via Kilo, Alfa", -> 13 "via Alfa"); SAAR partial (runway 20 end not connected: clearance without names); SARC no names in OSM. TODO: MSFS's own data via `tools/probe_taxi.py` (UNTESTED; needs the MSFS 2024 SDK SimConnect.dll via `--dll`, the bundled one has no facility API) -> `airports/msfs/ICAO.json`, preferred over OSM; hold short of crossed runways.
9c. **Rest of the IFR flight.** WORKING in the fake sim (2026-10-05, `flow.py`, `world.py`, all code-owned):
   - One run covers the flight: origin, destination and alternate are loaded (missing YAMLs generated from `data/`), the tuned frequency picks the airport; area control from `airspace/*.yaml` (`airspace/SAEF.yaml`: Ezeiza Control 135.5 first, 134.5, 125.2).
   - Telemetry watcher (1 s, in main `_Callbacks`) detects takeoff/landing and hands off: Tower -> Departure (700 ft AGL), Departure -> Control (FL100 / 30 NM), -> destination Approach (40 NM) or Tower (18 NM if no Approach), Approach -> Tower (12 NM), Tower -> Ground (vacated, < 40 kt). Said again once after 20 s if the frequency isn't changed; never while PTT is held.
   - Check-ins: "radar contact, climb via SID" / arrival "radar contact, expect runway 20, QNH ..."; wrong squawk -> "squawk 2235", then "radar contact" when the code shows.
   - Tower: takeoff ("wind ..., runway 31, cleared for takeoff" or "hold position, traffic on two miles final") and landing ("cleared to land" on final, "number two, traffic to follow..." or "continue approach, report final").
   - Fake sim: `/near SAAR 30 6000` puts you on an arrival.
   - After Valen's first real-sim flight (2026-10-05, `enroute.py`, tests/test_arrival.py), also code-owned:
     departure check-in "climb via SID to flight level 200" (filed level, no Control on file); "request direct X"
     only to route fixes still ahead (SimBrief navlog lat/lon), else "behind you" / "not on your route";
     "request higher"; descent at the navlog's TOD ("descend to three thousand feet, QNH ..., expect vectors runway 02",
     transition altitude from the OFP), also with the check-in if already past TOD; "request descent" before TOD ->
     "expect descent in N miles"; vectors to a point 10 NM out / 3 NM to the side, then "turn right heading 340,
     maintain 3000 until established, cleared approach runway 02"; Approach -> Tower once established; Tower clears
     to land on its own at 6 NM; after touchdown below 60 kt "welcome to Rosario, vacate via X when able" (first exit
     ahead on the taxi map, else backtrack). Destination without Approach (SAAR TWR/APP): Tower takes over at 40 NM
     and does the approach work. "On final ..." is a report that gets an answer, not a readback.
   - 2026-10-06: descent in two steps: area control "descend to FL100" (`enroute.CENTER_DESCENT_FT`), the arrival
     radar the final altitude (3000 ft, or ~2500 ft above a high field; never the transition altitude: US TA is
     18000); Center chosen near the aircraft (`World.control(own, airport)`: airspace files only within 700 NM of
     their reference point, else a CTR frequency the sim lists for the departure airport, e.g. KSFO "Oakland Center");
     station names the pilot uses ("SoCal Approach") are understood; only VHF airband frequencies (118-137 MHz) are
     ever used (OurAirports had a 36.07 "approach" at KLAX).
   - 2026-10-06 (2): one airspace file per Argentine FIR (SAEF Ezeiza, SARR Resistencia, SACF Cordoba, SAMF
     Mendoza, SAVF Comodoro Rivadavia; VATSIM Argentina manual): SARC departures go to "Resistencia Control"; a Tower
     with no Departure on file hands over to Control itself (2000 ft AGL or 5 NM). Worldwide Center from the FIR
     boundaries in little_navmap_navigraph.sqlite (`navdb.fir_at`: blob = big-endian int32 count + float32 lon/lat
     pairs, point in polygon; UIR frequency above FL245) when no airspace file is in reach: "Oakland Center",
     "Los Angeles Center". Center -> Center handoff en route when the FIR changes (hand files: nearest reference
     point). Speed control: vectors "reduce speed to two one zero knots", intercept 180, or 160 + "traffic to follow,
     Airbus three twenty on six mile final" when an AI on final is closer than 6 NM; speed is a readback item;
     indicated airspeed from `AIRSPEED_INDICATED` (VERIFY), ground speed if unknown.
   - 2026-10-06 (3): approach names from the sim's db with the chart suffix ("cleared ILS Zulu approach runway 13";
     the db's GPS rows with suffix A/D are STARs/SIDs, no longer counted as RNAV approaches), FAA order "cleared ILS
     runway two eight left approach"; STAR from SimBrief (`plan.star`, ICAO name from the route) -> area control
     "descend via the SERFR four arrival" (FAA; no level watched) / ICAO "descend via ASADO eight quebec arrival to
     flight level one zero zero". VFR flight following (`following.py`): "request flight following" / "flight
     information service" on a radar position -> squawk, "radar contact (ICAO identified), eight miles south of San
     Francisco, altimeter ...", then near the field "contact <X> Tower" or "radar service terminated, squawk VFR
     (7000), frequency change approved".
   - 2026-10-06 (4): holding on request (`holding.py`, published holds from the Navigraph db), runway requests
     (`flow.runway_request` + `runway.session_runway`: one place decides the runway for this pilot), "say again"
     repeats the last transmission word for word, radio/time checks and QNH/wind questions by code (`info.py`).
9d. **Airports load themselves** DONE in code (2026-10-06, `world.py`): no `--airport` needed with `--sim` (the
    airport you are on is found in the sim's scenery db, inside its area first; else airports/*.yaml, else
    OurAirports); spawning or tuning somewhere new loads that airport (a frequency no loaded airport has -> the
    nearby airport that has it, within 25 NM); a missing YAML is written once from OurAirports, or from the sim's db
    for add-on/fictional fields (`navdb.build_airport`), never over an existing file; generated files get a radio
    name (`gen.spoken_name`: "Heathrow", "Kennedy", "O'Hare", "Los Angeles"). Callsign from the sim's ATC settings
    when there is no plan (`SimConnectSource.identity()`, from the user's record in the AI list: VERIFY). Tests never
    write airport files (`world.AUTO_GENERATE` off in conftest). Fixed: Valen's "no station on 121.800" at KSFO.
9e. **US (FAA) phraseology** DONE in the fake sim (2026-10-06, KSFO -> KLAX flown with the real sim data): group-form
    callsigns ("United four thirty-six", ICAO stays digit by digit), levels by transition altitude (`phrase.level`:
    US flight levels from FL180, "one one thousand" without "feet"; Argentina above 3000 ft; TA from the sim's db),
    "climb and maintain"/"descend and maintain" (ICAO "climb to"/"descend to"), "wind 290 at 13", "then as filed",
    "readback correct, contact Ground point eight when ready", "push back approved", "contact ground point eight",
    after landing "turn left at Bravo, contact ground point eight" (no "welcome"), AI "taxi to the ramp".
    DONE 2026-10-06 (2): FAA clearance without a SID: "maintain five thousand, expect ... ten minutes after departure"
    (YAML `initial_alt_ft`, US default 5000; the readback must have it; the radar watches it until Departure climbs
    you); separate departure/arrival runways (YAML `runway_configs`, KSFO west plan land 28L/28R depart 1L/1R, SE plan
    19s/10s, picked by the arrival headwind; departures fall back to the arrival runways with > 5 kt tailwind; ATIS
    "landing runways ..., departing runways ..."); "hold short of runway X" for runways the computed taxi route
    crosses, then "holding short" -> "cross runway X" (or "hold short ..., traffic on two mile final") by position
    (`taxi.crossings`, `handle_crossing`); runway thresholds filled from the sim's db when the YAML has none (KSFO
    had none: parallel runways were all on the reference point); US parallel "R" runways default to right traffic.
10. **Session state, rest.** First slice DONE (`session.py`: telephony, clearance state, positions contacted). TODO: assigned runway, pattern position, last instruction per position.
11. **Runway-in-use logic.** DONE (`runway.py`). TODO: crosswind limits, preferred runways per airport, SABE noise abatement.
12. **Readback check.** DONE: runway / hold short / takeoff / landing / taxi route (`readback.py`) plus the full clearance (`clearance.py`).
    Fixed from Valen's first session (2026-10-05, replayed in tests/test_valen_session.py): an instruction read back
    correctly is not checked again (`session.acked_atc`); corrections don't nest; "three one via alfa" and "holding point
    for 31" are valid ICAO readbacks ("holding point" is a clearance limit, not an item; FAA "hold short" still is);
    echoed "when ready" isn't a request; "LATAM1302" is split for the callsign check; "finished pushback, ready to taxi"
    is a taxi request; "on holding point ..." to Ground -> "contact Tower" (also by telemetry when stopped there);
    a model reply that is only "<callsign>, roger" is dropped; learned telephony is kept in data/telephony_learned.json.
    Readback demanded (ICAO Doc 4444 4.5.7.5): "roger" to the IFR clearance (or push/taxi before reading it back) ->
    "read back the clearance"; "roger"/"wilco" to taxi, runway, takeoff/landing, hold short, QNH, squawk, climb/descend/
    heading -> "read back", and the next call is checked against that instruction (`session.readback_due`). QNH and
    squawk are now readback items. Handoffs, approvals and "hold position" need none.
13a. **AI traffic on the frequency** WORKING in the fake sim (2026-10-05, `tracker.py`, `chatter.py`,
    tests/test_traffic_handling.py). Viability: MSFS AI follows the sim's own ATC and can't be commanded, so we
    observe it and narrate: the tracker turns each AI's telemetry into events (taxi out, line up, takeoff roll,
    departed, final, vacated, taxi in) and Tower/Ground say the matching instruction + the AI's readback in its own
    Piper voice (libritts: 904 speakers). Radio bus: one channel, never while PTT is held, 4 s gap after any
    transmission, 6 s for the user's readback after ATC talks to them, stale (>15 s) dropped, newer instruction
    replaces an older one for the same aircraft. An AI on final is held ("continue approach, traffic on the
    runway") and cleared to land once the runway is free. Fake sim: `/aidep [rwy]`, `/aiarr [nm] [rwy]`.
    Real sim 2026-10-06: AI callsigns from ATC AIRLINE/FLIGHT NUMBER work ("United four three six, taxi to the
    apron", "Speedbird two eight six, runway two eight right, taxi via Mike one, Bravo, Foxtrot"). Quiet window after
    ATC talks to the user: 6 s (Valen). 2026-10-06 polish: pushback detected (moving tail first) -> "push and start
    approved" / FAA "push back approved"; departure handoff by name ("contact NorCal Departure ..."); FAA group form;
    accent voices (5b). 2026-10-06 (2): Approach/Departure chatter (AI checks in climbing through 2000 ft AGL ->
    "radar contact, climb ..."; arrival 7-15 NM -> "descend to ..., cleared ILS approach runway X" / "expect visual
    approach"; established inside 10 NM -> "contact tower ..."), one queued exchange per aircraft per position.
    2026-10-06 (3): ground conflicts with the user (`ground.py`, 30 s straight-line prediction, < 65 m): "give way
    to the Airbus three twenty from the left"; head-on "hold position, ... opposite direction" -> "continue taxi" once
    it is behind; progressive taxi ("request progressive taxi" / "unfamiliar with the airport": "I'll call your turns",
    then "turn left on Delta" ~150 m before each taxiway change, from the route's nodes).
13. **Traffic sequencing (VFR focus).** FIRST SLICE DONE (2026-10-05 audit, `sequence.py`, tests/test_sequence.py): code computes who is on the runway and on final for the runway in use (thresholds from OurAirports, now in the YAMLs), tells Tower whether takeoff/landing clearance is ALLOWED, and a guard replaces any "cleared for takeoff/to land" the model still gives ("hold position, traffic on two miles final" / "number two, traffic to follow..."). Rules: arrival inside 3 NM or anyone on the runway blocks takeoff; anyone closer on final or on the runway blocks landing. Fake sim: `/final 3 [rwy]`, `/onrwy`, `/notraffic`. TODO: pattern positions (downwind/base), departures still climbing out, line up and wait, verify with real AI traffic (own aircraft is now filtered by SimConnect object id, VERIFY).
    VFR circuit IN PROGRESS (2026-10-06): `src/atc/pattern.py` is written (inbound -> "join left downwind runway 31,
    wind, QNH, report downwind" / FAA "enter left downwind ..., report midfield downwind", straight-in when lined up;
    downwind -> number in sequence / FAA "number one, runway X, cleared to land"; base/final -> landing clearance or
    "cleared touch and go" / FAA "cleared for the option"; ready + "request left turnout" / circuits; watcher:
    after a touch and go "report downwind", a VFR departure at 8 NM "frequency change approved") and the session
    fields exist. WIRED IN 2026-10-06 (2), tests/test_pattern.py: a whole SABE circuit in the REPL makes no LLM
    call. Pattern calls are handled before the readback check ("left downwind 31" repeats Tower's words); the watcher
    clears a circuit aircraft on 2.5 NM final if it didn't call; a new downwind lap needs a new clearance; VFR full
    stops get "welcome, vacate via X"; no Departure handoff from the circuit or after a turn-out request ("VFR"),
    "frequency change approved" at 8 NM instead; zone transits ("request to transit the zone") stay with the model
    with a ZONE TRANSIT context line (never "cleared to land"). Fake sim: `/leg downwind|base|final [nm]|upwind|out`.
    Conditional line-up for the user (ICAO "behind the landing Airbus three twenty on two mile final, line up and
    wait runway three one, behind", "behind" is a readback item; FAA "hold short of runway X, traffic ...") and the
    takeoff clearance said by itself once the runway is free (`flow.takeoff_when_clear`). "N mile final" (not
    "miles").
14. **ATIS.** DONE in code (2026-10-06, `atis.py`): spoken on the ATIS frequency on COM1 (or COM2 if the sim says
    COM2 receive is on: `COM_RECEIVE:2`, VERIFY), one sentence per watcher tick so calls still get through, looped
    with a 3 s pause (text mode: once a minute), the ATIS position's own voice. ICAO and FAA formats; wind/QNH from
    the sim at the airport else the METAR; approach type from the sim's db; letter changes with the observation,
    runway or QNH. First call with an old letter -> "... Information Kilo is now current, QNH ..."; a letter heard
    before the ATIS was ever played is adopted. Checked with live METARs for SABE and KSFO in the fake sim.
14b. **Radar monitoring, go-arounds, emergencies** DONE in the fake sim (2026-10-06, `monitor.py`): level bust
    (past the cleared level by 300 ft in the cleared direction: "check altitude, maintain FL200"; not after the
    approach clearance), 7700 squawk ("emergency squawk observed, say nature of emergency and intentions"),
    unprompted traffic alerts (radar positions, converging within 6 NM / 1500 ft, once per aircraft, max one per
    45 s), Tower go-around on short final with the runway occupied ("go around, I say again, go around, Airbus three
    twenty on the runway"), pilot "going around" -> missed approach instruction and the arrival is flown again,
    "mayday"/"pan pan" -> "roger mayday, runway 02 available, wind ..., say intentions" (+ EMERGENCY facts for the
    model). Handoff readback with a wrong frequency -> "negative, contact Ezeiza Control one three five decimal five."
15. **Replay test set.** Harness DONE (6 scenarios, `silence_ok` / `expect_silence` supported). TODO: add SABE departure, pattern, go-around and traffic-conflict scenarios.

## D. Latency and cost (Phase 5)
16. **Timing logger.** PARTLY DONE: `[timing] stt + llm + tts` line printed when voice/STT is on. Code-owned replies (clearance, readbacks) take 0 s of LLM time.
17. **Streaming.** TTS DONE (2026-10-05): Piper plays sentence by sentence (the clearance starts after 1.2 s instead of 4.4 s; playback through `sd.OutputStream` not heard yet). LLM total deadline `ATC_LLM_DEADLINE_S` (default 12 s) across all fallbacks. TODO: LLM token streaming (most replies are code-owned now, so low value).
17b. **Fact check** DONE (`factcheck.py`): every LLM reply's numbers, aircraft types and taxiway names must appear in what the model was given; else one retry naming the problem, then "<callsign>, say again".
17c. **Traffic information by code** DONE (`traffic.py`): "traffic, two o'clock, three miles, opposite direction, Airbus three twenty, one thousand feet above" (within 8 NM / 3000 ft). AI type/airline/flight number read from SimConnect (`ATC MODEL`, `ATC AIRLINE`, `ATC FLIGHT NUMBER`; VERIFY with probe_traffic, falls back to the basic layout if rejected).
18. **Model comparison.** First pass DONE (2026-10-05) with `tools/compare_models.py` (Ground taxi turn at SABE, content checks + timing). Results on OpenRouter free:
    - `nvidia/nemotron-3-super-120b-a12b:free`: 2/2, about 0.9 s.
    - `nvidia/nemotron-3-ultra-550b-a55b:free`: 2/2, 1.2 s, but 12 s on a cold start. Valen uses it as main (commit 70be6f5).
    - `nvidia/nemotron-3.5-lightning:free`: invents taxiways ("via Alpha"). Don't use.
    - `google/gemma-4-*:free`: always 429 (rate-limited upstream).
    - `thinkingmachines/inkling-small:free`: 403, only for agentic apps.
    - Gemini and NVIDIA direct: not tested, no keys yet (`GEMINI_API_KEY`, `NVIDIA_API_KEY`).
    - Ollama (local): dropped. It made MSFS stutter and didn't answer; uninstalled.
    - TODO: Claude Haiku 4.5 when paying; check the prompt-caching minimum size.
    - LIMIT FOUND (2026-10-05): OpenRouter free = 50 requests/day TOTAL across all `:free` models (1000/day after a
      one-time 10 credit top-up). About 10-20 LLM calls per flight, so 2-4 flights/day. The client now stops on a
      daily-quota 429 instead of retrying every free model.
    - Latency seen: nemotron ultra 0.9-6 s, once 17.5 s (the 10 s timeout is per socket read, not total).
    - Multi-provider (2026-10-05): `ATC_LLM_MODEL="groq:...,gemini:...,openrouter:..."`, each with its own key
      (`GROQ_API_KEY`, `GEMINI_API_KEY`, `CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`,
      `ANTHROPIC_API_KEY`; read from the Windows user environment too). Free quotas stack. With clearance, taxi,
      takeoff/landing, check-ins, handoffs and traffic in code, a flight needs only a few LLM calls (VFR pattern work,
      questions). `tools/compare_models.py` scores those LLM-owned turns (VFR inbound SARC, wind/QNH question SABE).
    - Free-key results 2026-10-05 (2 runs x 2 turns, with fact check + landing guard on):
      4/4 groq:qwen/qwen3.8-27b 0.7 s (but says "cleared to land" 10 NM out: the guard fixes it to "report final"),
      4/4 nvidia:nvidia/nemotron-3-super-120b-a12b 1.0 s, 4/4 gemini:gemini-flash-lite-latest 1.1 s,
      4/4 openrouter nemotron-3-super:free 1.1 s, 4/4 gemini:gemini-3.5-flash 4.9 s.
      Without the guard groq gpt-oss-20b/120b scored 0/4 (early landing clearance, "wind three zero" for 030).
      Gone/404 now: gemini-2.5-*, nvidia meta/llama-3.3-70b. Groq needs a User-Agent (Cloudflare 403 1010).
      Gemini thinking off = reasoning_effort "minimal" ("none" is a 400 on 3.5 Lite). Recommended order:
      gemini flash-lite-latest, nvidia nemotron super, openrouter nemotron super :free, groq qwen3.8.
    - Winds are said magnetic where `mag_var_deg` is in the YAML (SABE -10 from the LIDO chart); SARC/SAAR TODO.

## E. Coverage (Phase 6) and extras
19. SABE/SARC manual pass from the AIP charts: taxi routes, real departure frequency (SABE currently uses APP 120.6 as the departure frequency, VERIFY), procedures, reporting points.
20. Spanish phraseology prompt and Piper Spanish voice. Then Norwegian for practice.
21. Optional Phase 7: VATSIM "who covers me" helper (see docs/PLAN.md).

## Risks to keep in mind
- Free model tiers rate-limit at busy hours (Gemma already does). Keep a fallback model in `ATC_LLM_MODEL`.
- LLM output varies per run, so tests check content rules, not exact strings. Code-owned replies are exact and tested exactly.
- STT mistakes on callsigns/numbers turn into false "negative" corrections. Watch for this when voice input is tested.
- Piper quality is good for a prototype but not identical to cloud neural voices.
