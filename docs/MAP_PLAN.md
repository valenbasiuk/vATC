# Live map + world traffic: action plan (2026-10-08)

Valen asked for a map in the app, like VATGlasses: which Center frequency you are about to enter, the traffic around
you, and traffic that exists far away (flying real routes) but only enters the sim within 40 NM. Viable: everything
it needs is already in the app (data/centers.json, the status snapshot, own traffic's kinematics and injector).
Plan only: work it phase by phase, each phase ends with a flight in the sim before the next one starts.

Already built (2026-10-08, the first piece of phase 1): `centers.ahead()` (next sector along the track and its
distance), `/center` in the REPL, the launcher's live panel line "Center: X 128.400  next: Y 124.300 in 34 NM",
and `/fly LAT LON ALT HDG [GS] [xRATE]` (fake sim: in the air anywhere, flying straight on).

## Shape: a local web page, not a tkinter canvas
- The ATC process (`python -m atc`) serves a page on http://127.0.0.1:8740 (standard library `http.server` in a
  thread: no new dependency). The launcher gets a "Map" button that opens it in the browser; with two screens it
  sits next to the sim.
- The page: Leaflet (from cdnjs) on OpenStreetMap / CARTO dark tiles, one HTML + JS file in `src/atc/web/`.
  Polls `/state.json` once a second; sectors come as GeoJSON per viewport.
- Why not tkinter: a map widget there (tkintermapview) is slow with thousands of polygons and has no level filter
  or styling to speak of; the browser does this for free, and works on a phone on the same Wi-Fi (bind 0.0.0.0
  behind a launcher option).

## Phase 1: the sector map (VATGlasses-style)
- `/sectors.geojson?bbox=...&alt=...`: the Center volumes of data/centers.json in view that contain that level,
  simplified (Douglas-Peucker, ~200 m) and cached per (bbox cell, level band). Each feature: callsign, frequency,
  source (VATGlasses / vNAS / Navigraph / hand), floor/ceiling.
- Page: sectors coloured per position (the same colour for one frequency), labels "Asuncion Control 128.400";
  the one you are in filled, the next one along your track outlined, with "in 34 NM / 4 min". A level slider
  (default: your altitude) shows the upper / lower splits. Click a sector: callsign, frequency, levels, source.
- Own aircraft: icon by heading, track line ahead 10 min, the tuned station on COM1 (green when it is the right one
  for where you are, amber when a handoff is due, red when nobody answers there).
- Airports loaded in the world: dot + their frequencies on click (ATIS letter when there is one).
- Done when: flying SABE -> SGAS (fake sim `/fly` and MSFS) the map shows each sector boundary before the handoff
  and the frequencies match what the controller says.

## Phase 2: traffic on the map
- `/state.json` gains every aircraft the app knows: the sim's AI (SimConnect reader), ours (atc.own: state, route
  ahead), with a `source` field; drawn with different icons, labels "ARG1234 B738 FL350 450kt".
- Click: type, airline, origin/destination when known, our pilots' state (taxiing, holding, final...), and for ours
  the taxi route / approach path as a line.
- Ground view at an airport: taxiway names from the taxi map, stands, holding points (helps check the scenery).
- Done when: at SABE with --own-traffic every aircraft on the apron and on final is on the map where it is in MSFS.

## Phase 3: world traffic (virtual, beyond 40 NM)
A new module `atc/own/world.py`: flights that exist without being in the sim.
- Where they come from: FS Traffic schedules (already read by atc.own.schedule) for every airport within ~400 NM of
  you: departures that left in the last hours and arrivals due in the next ones, each with origin, destination,
  type and departure time.
- How they fly (1 Hz, no SimConnect): great circle origin -> destination (phase 5 can use real airways and SID/STAR
  fixes from the navdata), climb at the type's rate (atc.own.motion.perf) to a cruise level by direction (east odd,
  west even), cruise speed by type, top of descent at 3x the height, 250 kt below FL100, joining the existing
  arrival model (Approach) at 12 NM final.
- Into the sim at 40 NM: a virtual flight within INJECT_NM (40) of you is created in the sim at its position, level,
  heading and speed (the injector already creates airborne arrivals: gear heights, freeze, per-frame poses) and from
  then on moved by the same kinematics; beyond 45 NM it is removed again (hysteresis), still on the map. Departures
  of the airport you are at start at the gate as today and become virtual once beyond 45 NM.
- Budget: at most N in the sim at once (default 15, a launcher setting) for FPS; the nearest first.
- Separation: virtual flights keep 1000 ft / 5 NM from each other and from you (vectors/level changes by code, the
  same rules the radar watch uses); arrivals are sequenced by the existing runway controller before they reach 40 NM.
- Done when: an hour at SABE shows a steady stream on the map, the ones within 40 NM appear in MSFS where the map
  has them (no pop-in jumps), and FPS doesn't drop more than with today's own traffic.

## Phase 4: radio for world traffic
- Center frequencies come alive: a virtual flight in the sector you are tuned to checks in ("Asuncion Control,
  Aerolineas 1234, flight level three five zero"), gets "radar contact", and is handed to the next sector by the
  same sector data ("contact Resistencia Control one two four decimal three"), through the RadioBus (gaps, no talking
  over you). Approach / Departure chatter for the ones near the airport (exists for sim AI today).
- Traffic information to you uses them too (they are real targets on the map).
- Done when: a 30-minute cruise on one Center frequency has believable chatter, never over your own calls.

## Phase 5: oceanic and polish
- Oceanic: position reports by code ("Shanwick Radio, Speedbird 123, position 55 north 020 west at 1235, flight
  level 350, estimating 56 north 030 west at 1310, next 57 north 040 west"), the oceanic clearance before the entry
  point (route, level, Mach), SELCAL check; the map shows the NAT tracks of the day (from the NAT track message
  that ganderoceanic.ca / nattrak publish).
- Real airways for virtual flights (navdata), SID/STAR entry points, holding stacks drawn when ATC holds traffic.
- Done when: a KEF -> JFK crossing has the oceanic clearance, position reports and the Gander / Moncton handoffs.

## Risks
- FPS: the map is in the browser (no sim cost); the cost is the number of aircraft in the sim (phase 3 budget).
- Pop-in: an aircraft appearing at 40 NM must already match the map (same kinematics, created at the exact pose).
- FS Traffic / sim AI must be off with world traffic (two traffic sources at once = duplicates).
- Data: data/centers.json is personal-use (CC BY-NC-SA): the map never leaves the PC unless Valen turns on LAN access.
