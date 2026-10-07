# Own traffic + better voices: action plan (2026-10-07)

Valen asked for (1) our own traffic injector whose aircraft obey this ATC, and (2) better voices. This file is the
plan; work it phase by phase, each phase ends with a flight in the sim before the next one starts.

## Why our own traffic
MSFS AI (generated or live, and FSLTL / FS Traffic too) is flown by the sim: it picks its own runway, taxis through
the user, ignores every instruction of this ATC. FSLTL's injector only creates the aircraft (SimConnect) and then
"the sim is in control of the movement" (FSLTL user guide). So our ATC can only describe what the sim AI does.
With our own aircraft the ATC decides and the aircraft do it: hold short, line up behind the user, give way, land on
the runway in use, vacate where told, taxi the route Ground gave.

How: the vPilot method (VATSIM clients show other pilots this way). Create a non-ATC aircraft, release it from the
sim's AI, freeze it, and set its position / attitude ourselves many times a second. No sim physics: a kinematic model
(accelerate, rotate, climb, turn, 3 degree glide path, flare, brake) is enough seen from a cockpit.

## What Valen already has (checked on his PC, 2026-10-07)
Community folder: `%LOCALAPPDATA%\Packages\Microsoft.Limitless_8wekyb3d8bbwe\LocalCache\Packages\Community`
(from UserCfg.opt `InstalledPackagesPath` + `\Community`).
- `fsltl-traffic-base`: 2550 aircraft folders, legacy (FS2020-format) AI models. One title per livery in
  `aircraft.cfg` `[FLTSIM.0] title=...`, with `icao_airline`, `[GENERAL] icao_type_designator`.
  `FSLTL_Rules.vmr` (vPilot model matching, 118k rules):
  `<ModelMatchRule CallsignPrefix="FBZ" TypeCode="B738" ModelName="FSLTL_FAIB_B738_FBZ-FlyBondi" />`;
  ModelName is the exact title; `A//B` = pick one; rules without CallsignPrefix are the type fallback
  (`FSLTL_A320_ZZZZ` = white livery). Argentina is covered: ARG B738 / B737 / E190 / A332, FBZ B738,
  JES A320 ("Smartbird"), LAN / LPE.
- `justflight-fstraffic-module` (FS Traffic): 41 models with many liveries each (`Title=JustFlight_AI_737-800_
  FBLineasAereasS.A.FBZ`, `icao_airline=FBZ`); `Data/Aircraft/aircraftIniFiles/<IATA type>.ini` maps
  `[ICAO airline] title=/title2=...` with a `[fallback]` white one.
  `Data/Schedules/<ICAO>.ini`: real departures per airport and weekday, e.g. SABE
  `DT:0405,ICAO:SBBR,CA:ARG,AC:738,FLTNO1216,CS:ARGENTINA,DAYS:25*` (7413 airports; SABE 1127 lines).
  `Data/DBs/aircraftCodesDB.csv` (IATA 738 -> ICAO B738), `airportsTMZ.ini` (time zones: DT is probably local
  time, VERIFY).
- Both are legacy packages: a title alone creates the right livery with `SimConnect_AICreateNonATCAircraft`
  (works with the SimConnect.dll we bundle). MSFS 2024 modular aircraft would need
  `SimConnect_AICreateNonATCAircraft_EX1(title, livery, ...)`, which is in the 2024 SDK DLL only: not needed here.
- Sim db (Little Navmap): parking spots with type / radius / heading (SABE: 56, `airline_codes` empty), taxi paths.
  We read these files locally only; nothing from FSLTL / FS Traffic is ever copied into this repo.

Sim settings while our traffic runs (FSLTL's own advice for an injector): Traffic type OFF, aircraft traffic
quantity OFF, parked aircraft OFF (or low). Then every aircraft around is ours (plus parked ones, which don't move).

## Status
- Phase 0 PASSED in MSFS (2026-10-07, SABE): sim-AI model and `FSLTL_FAIB_B738_FBZ-FlyBondi` both created,
  moved 60 m + turned 90 deg at 20 Hz smoothly, ended 0.3 m from the commanded spot, removed. Valen saw all of it.
- Phase 1 BUILT, fake sim + tests only (tests/test_own_traffic.py; a whole SABE departure simulated with the real
  taxi map: push 23 s, taxi via Mike, Alfa, takeoff on 13 at ~5 min). `src/atc/own/`, run with `--own-traffic`.
  WATCH IN THE SIM (first flight with it, sim traffic OFF):
  - height: on its gear while taxiing (not sunk / floating)? `PLANE ALT ABOVE GROUND` of a frozen object is what
    the injector follows (injector.py).
  - pitch on rotation: nose up? If it pitches DOWN, run with `$env:ATC_OWN_PITCH_SIGN = "1"`.
  - gear up after takeoff, lights, engines (events on a non-ATC aircraft: may do nothing).
  - its ATC id: does the sim report our callsign ("ARG1216") for the object? (own.merge also drops anything within
    20 m of one of ours, so a different id only matters for the chatter tracker.)
  - SimConnect exceptions printed as `[own traffic: SimConnect exception ...]`.
  - pushback looks right (tail first along the lead-in, then along the taxiway)? stops behind you on the taxiway?

- First flight with it (2026-10-07, SABE): taxi and waiting behind others OK, takeoff sequencing OK; rough /
  laggy (worst on pushback), stopped a bit past the holding point, braking not smooth, no landings, parked sim
  aircraft still visible (sim setting), FSLTL's Aerolineas logo looked stretched, no tug. Then built (fake sim +
  tests, 256 tests; 5 x 40 min simulated at SABE: 0 runway conflicts, 0 landings without clearance):
  - smoothness: telemetry read on its own thread (it blocked the mover for up to 1.5 s); poses sent once per sim
    frame ("Frame" event + Win32 event handle); sim clock stops on "Pause" and runs at the sim rate; terrain
    height eased in (no bump once a second).
  - taxi 15 kt on taxiways, 10 kt on aprons, 7 kt in turns, never above 20; braking planned at 0.45 m/s2 through
    a jerk-limited controller; stops 60 m behind traffic; nose stops 6 m short of the holding point.
  - phase 2 arrivals: 12 NM final (behind whoever is on it), Tower call at 9 NM, landing clearance or "continue
    approach" (traffic on the runway / departing / number two), go-around at 1 NM without one; touchdown ~400 m,
    braking dosed for the exit planned at touchdown on the stand's side of the runway; "contact Ground", taxi to
    the stand, parked 4-8 min, removed. Departures wait for arrivals inside 5 NM.
  - traffic amount: `schedule.movements_per_hour` (FS Traffic deps today x2 / 16 h, boost x3 under 20/day, x1.5
    under 150, x2 more at 3000 m+ runways; min 6/h, max 40/h; GA only without schedule: 3/h towered, 1.5/h not),
    airport YAML `traffic_per_hour` overrides, `--own-factor`. SABE 22/h, SAEZ 20/h, SARC/SAAR 6/h, JFK 40/h.
  - liveries: FS Traffic's first (ATC_OWN_MODELS=fsltl for FSLTL first). Tug: GSX's FSDT_TPX_200 / TPX_500 under the
    nose during the push (AICreateSimulatedObject; ATC_OWN_TUG=<title>|off, ATC_OWN_TUG_YAW if it faces wrong).
  - push held by Ground while someone taxis behind the stand; the push stops for traffic.
  - gear heights per type learned from ground spawns (data/gear_heights.json) for the ones created in the air.
  WATCH IN THE SIM next: smoothness per frame; arrivals (sunk/floating after touchdown = gear height guess); the
  tug (appears? faces right? moves with the nose?); the Pause event; exits taken / stands reached.

## Phases

### Phase 0 - can we move an aircraft smoothly? (`tools/probe_inject.py`: PASSED)
Valen runs it standing on an apron. Pass: the aircraft appears on the ground 120 m ahead (not sunk / floating),
slides 60 m and turns smoothly at 20 Hz, is removed. Also try `--title "FSLTL_FAIB_B738_FBZ-FlyBondi"` (a FSLTL
title, not one copied from the sim's AI).
- If it jumps / shakes: 0b, updates synchronised to the sim frame (subscribe to the "Frame" system event, send
  one position per frame from a dedicated thread), plus velocity simvars.
- If it falls back to where it was (the sim keeps control): check the freeze events and AIReleaseControl order.
- Fallback B if position setting can't be made smooth: let the sim fly it on our waypoints (`AI WAYPOINT LIST`,
  ground waypoints for taxi, our speeds), stop it by withholding the next waypoint. Smoother, less control.
Decision gate: no smooth movement, no project (then: keep sim AI on Low and the ATC follows it, as now).

### Phase 1 - one departure, end to end (1-2 sessions)
New package `src/atc/traffic_own/`:
- `models.py` model catalog: scan the Community folder once (FSLTL VMR + FS Traffic ini files + aircraft.cfg
  titles), cache `data/models_cache.json`; `pick(airline_icao, type_icao) -> title` (exact, then same airline any
  type, then type white livery). Per type: wingspan class, rotate speed, climb rate, approach speed (small table:
  A320 family, B737 family, E190, ATR, A330/B767/B787/B777, B747/A380, regional jets, GA).
- `motion.py` kinematics: a path (list of lat/lon/alt/speed points) -> position, heading, pitch, bank at time t.
  Ground: speed limits (taxi 15 kt, 8 kt in turns, push 2 kt backwards), smooth turns along graph nodes.
  Air: takeoff roll (accel to Vr, rotate 2.5 deg/s to 8-10 deg pitch), climb 2000 ft/min, turns at 25 deg bank.
- `injector.py` SimConnect side (from the probe): create, release + freeze, set position at the update rate, gear /
  lights / engines on (simvars/events: VERIFY which work on a non-ATC aircraft), remove. Height above ground:
  read the object's `PLANE ALT ABOVE GROUND` at 1 Hz and correct (terrain under taxiways isn't flat).
- `pilot.py` per-aircraft state machine: parked -> pushback -> taxi (route from `taxi.py`, the same graph Ground
  uses, so "via Delta, India" is the path actually flown) -> hold short -> line up -> takeoff -> climb out ->
  removed at 12 NM / 6000 ft. Every step waits for its ATC clearance (code-owned, from the shared runway
  controller below).
- REPL / fake sim first: the fake sim gets the same injector interface, so the whole state machine is tested with
  pytest (positions, timings, clearances) before the sim sees it.
Done when: in MSFS at SABE, one Aerolineas 737 pushes from a stand, taxis the route Ground read out, holds short,
lines up, takes off on the runway in use, climbs out, disappears; the radio chatter matches what it does.

### Phase 2 - arrivals (1 session)
- Spawn at 15-20 NM on the extended centerline (or a simple base turn from the STAR side), 3000 ft, 180 kt;
  slow to approach speed at 6 NM, gear at 5 NM, 3 deg glide path, flare at 30 ft, touchdown, brake to 20 kt.
- Vacate at the exit `taxi.vacate()` picks (the one Tower says), taxi to a free stand (`taxi.free_stand`, type and
  radius from the sim db parking table), park, engines off, removed after a few minutes (or turn around later).
- Approach / Tower chatter from the state machine, not inferred from positions.
- Go-around when the runway isn't free at 1 NM (the user lined up, a slow departure).
Done when: arrivals land on the runway in use, vacate, taxi in, and never land on the user.

### Phase 3 - living with the user (1-2 sessions): one controller for everybody
- `runway_control.py`: one queue per runway for departures at the holding points (user included) and arrivals on
  final. Tower's decisions for the user (existing code in flow.py / sequence.py) and for our aircraft come from it:
  "number two for departure", line up behind, conditional line-up behind landing traffic (ICAO), departure
  spacing (previous one airborne and 1.5 NM / 2 min, wake turbulence for heavies), arrival spacing (3-4 NM).
- Taxi conflicts: our aircraft reserve graph edges ahead of them and stop behind anyone (the user too) closer than
  60 m ahead on their path; at crossings the one with priority (ATC decides, `ground.py` logic) goes first.
- The user's own taxi clearance can then name real traffic: "give way to the Aerolineas 737 from the left".
- The chatter tracker ignores our aircraft (`tracker.py` would otherwise re-infer events for them).
Done when: Valen taxis to the holding point in a queue of 2-3 of ours, nobody drives through anybody, Tower runs
departures and arrivals on one runway with him in the sequence.

### Phase 4 - how much traffic and whose (1 session)
- Schedule: FS Traffic `Schedules/<ICAO>.ini` departures for today's weekday around the current time (local / UTC
  VERIFY); arrivals from an index of every schedule line with `ICAO:<here>` (built once, cached), arrival time =
  departure + distance / cruise speed. Without FS Traffic: a per-airport YAML list of airlines / types, random times.
- Density setting (`--traffic low|medium|high` = share of the schedule), a bubble around the user (origin airport
  while on the ground there, destination airport within 60 NM; nothing en route at first).
- Parked aircraft at free stands (static, our own, cheap) so the apron isn't empty with sim traffic off.
- Callsigns from the schedule (`CS:ARGENTINA` + flight number; telephony from data/airlines.dat), voices by
  airline country (`voices.py` already does this).

### Later (not planned yet)
Turnarounds (an arrival becomes a departure), en route traffic for traffic information, VFR circuit traffic at
SARC, other airports than the user's, helicopters.

## Risks
- Smoothness of 20-60 Hz position updates from Python (phase 0 decides).
- Animations on a frozen non-ATC aircraft: gear / flaps / lights / engines may need events or may not work at all.
  Acceptable: gear down on the ground and on final, lights on; flaps nice-to-have.
- SimConnect's 1000 object limit (FSLTL FAQ): irrelevant at our numbers, but heavy scenery + vehicles count too.
- CPU: Python + SimConnect calls for 10-20 aircraft is light; measure with the sim running (Ollama made it stutter).
- Parking: SABE's default parking has no airline codes; types/radius are enough to choose a stand.

## Voices (separate track, can go between flights)
Valen doesn't like the current Piper voices. Options, all behind the existing TTS interface (`audio/tts.py`):
1. Better Piper voices: there are more English ones (en_GB alan / northern_english_male, en_US ryan / lessac ...);
   `tools/voice_samples.py` + `voices/blacklist.txt` already exist. Cheapest, same latency.
2. Kokoro-82M (ONNX, local, Apache-2.0): much more natural than Piper; American (20) and British (8) English
   voices, Spanish voices for the later Spanish ATC. On CPU about real time or a little faster: fine with the
   sentence streaming we have, but measure first audio with MSFS running.
3. Microsoft Edge neural voices (`edge-tts`, cloud, free, no key, unofficial endpoint that may break): very natural,
   many English accents (US, GB, AU, IN, IE, ZA, NZ ...) and es-AR; ~0.5 s network latency. Fits "a voice per
   country" best. CLAUDE.md said cloud voice only if Piper isn't enough: Valen says it isn't.
Step V1: `tools/voice_compare.py` renders the same 6 ATC lines with each engine (and a few voices each) through the
radio filter into voice_samples/compare/, printing time to first audio. Valen listens and picks.
Step V2: wire the winner(s) into the voice bank (controllers and pilots can come from different engines), keep
Piper as the offline fallback. Also tune the radio filter: a lot of "robotic" sounds comes from it.
