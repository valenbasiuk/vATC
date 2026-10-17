# Next session plan (written 2026-10-11, after improvement plan blocks 4-7 and the full code review)

> 2026-10-15: superseded in part. Current state: CLAUDE.md "Where we are"; what to check in MSFS first:
> docs/ROADMAP.md "WATCH IN THE SIM 2026-10-15"; next build work: ROADMAP "NEXT" (block 5). Step 3.2 below (ATC-initiated
> holding) is DONE (holding.sequence_event).

Start here after a `/clear`: read CLAUDE.md (auto-loaded), then this file, then work the steps in order.
State: 365 tests pass, 6/6 scenarios pass. Everything since commit 1d26ed1 is uncommitted on disk (`git status`):
Valen commits himself; don't commit unless he asks.

## Done 2026-10-08 .. 10-11 (fake sim + tests only; nothing of it flown in MSFS yet)
- Improvement plan blocks 1-7 (docs/IMPROVEMENT_PLAN.md): ATIS on a clock, the launcher, one runway controller,
  approach work (visual, ATC holds, MSA, missed approach, deviations), controller behaviour (readback modes,
  "blocked", busy "standby"), emergencies, LVP, own-traffic extras (turnarounds, joins, circuits, CTAF, toward the
  destination), side-step, Class Bravo, ride reports, closures, intersection taxi, the voice comparison tool and
  Spanish radio. What each does: docs/ROADMAP.md "BUILT 2026-10-11".
- Code review of every module: race conditions fixed (the AI reader shared by two threads, the departure queue,
  the ATIS letters), the launcher's error messages (crashed instead of showing), many small fixes.
- Soak test of our traffic (tools/soak_own_traffic.py: the manager + the watcher on the sim clock, 3 simulated
  hours at SABE and SARC with the real scenery and FSLTL/FS Traffic data): push deadlock, starved departures, burst arrivals,
  go-arounds after a backtrack found and fixed; "line up and wait" for our departures added. Now: nothing stuck,
  no go-arounds, the longest wait at the holding point ~200 s.

## 1. Valen flies (brings the launcher's Flight-tab log or the terminal output)
Read docs/ROADMAP.md "WATCH IN THE SIM 2026-10-11" first (then the 2026-10-08 lists): every feature built without
the sim, what may go wrong in MSFS, and the constant to tune. Match what Valen brings against it.
Suggested flights, each short:
1. SABE IFR departure with own traffic (launcher, own traffic on): our departures' "line up and wait" and the
   conditional behind a landing one; arrivals ~3-4 min apart; turnarounds after 45-90 min.
2. An IFR arrival into SABE or SAEZ with own traffic busy: ATC hold when 3+ are ahead, the visual approach on
   "field in sight", a go-around for the missed-approach handling.
3. SAMR or Mendoza area (MSA): descents never below the sector MSA, the low altitude warning if he dives under it.
4. A mayday on the climb out ("engine failure, request return").
5. SARC VFR circuits with our Cessnas in the circuit; then a field without Tower for the CTAF calls.
6. `--language es` once: station names and phrases (VERIFY list in src/atc/spanish.py).
7. `python tools/voice_compare.py` (optional installs) -> pick voices into voices/extra_voices.txt.
Carried over, still never seen in MSFS: STT + PTT timing (`[timing]` lines), radio filter by ear, ATIS on COM2,
callsign from the sim's ATC settings, spawn at a random airport (YAML written?), AIRSPEED_INDICATED in knots,
AMBIENT_VISIBILITY in metres, tools/probe_lights.py (which way lights our aircraft).
Fix what comes back before adding anything new.

## 2. Questions for Valen (don't guess)
- Spanish station names as Argentine controllers say them: Ground "Superficie"? Delivery "Autorizaciones"?
  Departure "Salidas"? (src/atc/spanish.py STATIONS, marked VERIFY).
- SARC approach: the sim's db gives APP 118.1 / 118.7 "Resistencia"; the VATSIM manual has SARE_APP 119.4
  "Resistencia Control". Which does MSFS show at SARC? Only then change SARC.yaml.
- VFR reporting points (SABE river points, SARC): not in any database here; joins via a reporting point (and
  improvement plan E2) need them by hand (AIP VAC) in the airport YAML.
- SABE circuit: the YAML says "ASSUMED: pattern 1000 ft AGL, left traffic"; real Aeroparque VFR procedures differ.
- With arrivals now spaced by time (3 min, 4 min when a departure is coming), is SABE busy enough? `--own-factor`.

## 3. Next items (after the flights, unless Valen asks otherwise)
1. Fix what the real sim shows.
2. docs/MAP_PLAN.md: the live map (VATGlasses-like) and world traffic beyond 40 NM (plan only so far).
3. F2 radio failure (7600), E2 Argentine CTR entry/exit with reporting points (needs the points), named visuals.
4. Norwegian radio (Valen practices it): the Spanish layer is the pattern (a table both ways + a speaker wrapper).
5. Check "via <fix>" against the approach's transitions in the sim db (`transition` table) and say the IAF.

## Notes for whoever picks this up
- Git Bash heredocs on this PC eat backslash-newline line continuations even with a quoted delimiter
  (`<<'EOF'`), and Python heredocs mangle `\b`/`\d` in replacement strings. Edit code that has either with the Edit
  tool, or write the patch script to a file first and run it.
- Git Bash turns arguments starting with "/" into paths (`/freq` -> `C:/Program Files/Git/freq`): set
  `MSYS_NO_PATHCONV=1` when driving the REPL with "/" commands from bash, or drive it from a Python subprocess.
- Tests run with `world.AUTO_GENERATE = False`, `ATC_METAR=off` and a fake `ATC_LNM_DB` (conftest): no navdata FIRs
  and no sim db in normal tests. tests/test_navdb.py opts back in and runs only where the databases exist.
- `runway_in_use(..., use="departure"|"arrival")`: pass `use` at every new call site.
- Soak-testing our traffic: `python tools/soak_own_traffic.py [--airports SABE,SARC] [--hours 3] [--seed N]
  [--fake-data] [--log]` (the manager + the watcher on a sim clock, the real scenery and FSLTL / FS Traffic data).
  Run it after any change in src/atc/own/ or departures.py: it reports aircraft stuck > 15 min in a state, one
  running into another on a runway, every go-around with who was on the runway, the longest holding-point waits.
- Python deletes the `except ... as exc` name when the block ends: a lambda run later (tkinter `after`) must bind
  it (`lambda msg=str(exc): ...`).
