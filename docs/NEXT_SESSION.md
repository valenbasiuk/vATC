# Next session plan (written 2026-10-06, end of the second session that day)

Start here after a `/clear`: read CLAUDE.md (auto-loaded), then this file, then work the steps in order.
State: 203 tests pass, 6/6 scenarios pass. Valen committed part of this session as "wait for takeoff" (46f765e);
the rest (speed control, worldwide FIRs, voice samples, docs, ...) is uncommitted on disk (`git status`). Don't
commit unless Valen asks.

## Done this session (all fake sim / sim db; nothing flown in MSFS yet)
- VFR circuit wired in (`pattern.py`, tests/test_pattern.py): a whole SABE circuit in the REPL makes no LLM call.
  REPL `/leg downwind|base|final [nm]|upwind|out` places you in the circuit.
- Argentine FIR files (airspace/SARR, SACF, SAMF, SAVF): SARC departures -> "Resistencia Control". Tower with no
  Departure on file hands over to Control itself. Worldwide Center from the Navigraph FIR boundaries
  (`navdb.fir_at`), Center -> Center handoffs en route.
- FAA "maintain five thousand" (no SID), runway crossings ("hold short of runway 28R" -> "cross runway 28R"),
  runway thresholds from the sim's db (KSFO had none), `runway_configs` (KSFO departs 1L/1R, lands 28L/28R).
- Approach/Departure chatter for AI; ICAO conditional line-up + takeoff clearance said once the runway is free;
  speed control on the arrival (210 / 180 / 160 + traffic to follow); tools/voice_samples.py + voices/blacklist.txt.

## 1. Things only the real sim can tell (Valen flies, brings the terminal output)
Ask Valen to run (voice, PTT, no --airport, no --simbrief first, then with it):
`python -m atc --sim --voice voices/en_US-libritts-high.onnx --ptt [--ptt-joy N --ptt-joy-device D]`
- Carried over: spawn at a random airport (YAML written? Ground answers?), callsign from the sim's ATC settings,
  ATIS on COM1/COM2, voices by ear, pushback detection with real AI, radar monitoring false alarms, `[timing]` STT.
- New: a VFR circuit at SABE or SARC (Tower 118.85 / 118.3): join, downwind number, touch and go, "report downwind"
  after it, full stop + vacate. Does the downwind report come out right with real STT?
- New: KSFO taxi from a gate to 1L: "hold short of runway two eight right"; stop at the line and say "holding short
  of runway two eight right": "cross runway ...". The position check is 0.25 NM from the crossing point: too tight
  or too loose with real taxi data?
- New: a departure that climbs out of an FIR (KSFO -> KLAX at FL300): Oakland Center 127.8, then the upper
  frequency above FL245, then Los Angeles Center. These are AIRAC 1801 frequencies: do they match what MSFS shows?
- New: speed control: does `AIRSPEED_INDICATED` come back in knots? `tools/probe_own.py` prints `ias_kt` now.
- New: Approach chatter for real AI arrivals (on the Approach frequency): too chatty? Wrong runway?
- `python tools/voice_samples.py` once, listen, put bad voices in voices/blacklist.txt (or `accent:Spanish` if the
  Argentine controllers are too hard to understand).
Fix what comes back before step 3.

## 2. Questions for Valen (don't guess)
- SARC approach: the sim's db gives APP 118.1 / 118.7 "Resistencia"; the VATSIM manual has SARE_APP 119.4
  "Resistencia Control". Which does MSFS show at SARC? Only then change SARC.yaml.
- SABE circuit: the YAML says "ASSUMED: pattern 1000 ft AGL, left traffic". Real Aeroparque VFR procedures differ
  (river reporting points); ask what he flies, or take it from the AIP VAC.
- KSFO `runway_configs` (west plan 28s/1s, SE plan 19s/10s) were written from general knowledge: fine for him?

## 3. Next realism items (in this order unless Valen asks otherwise)
1. Ground conflicts: AI taxiing across the user's route -> "give way to the Airbus from the left" / "hold position".
2. Approach procedures by name from the sim's db ("cleared ILS Z runway 13"), STAR in the clearance/descent.
3. Holding when the runway stays blocked (needs a hold fix: the STAR's last fix or the IAF).
4. Progressive taxi on request ("request progressive"): turn-by-turn from the route nodes (taxi.route_path).
5. VFR flight following in the US ("squawk 4521, radar contact, altimeter ...") and VFR departures from Class B.
6. Spanish phraseology + Spanish ATC voice (roadmap 20) once Valen wants it.

## Notes for whoever picks this up
- Git Bash heredocs on this PC eat backslash-newline line continuations even with a quoted delimiter
  (`<<'EOF'`), and Python heredocs mangle `\b`/`\d` in replacement strings. Edit code that has either with the Edit
  tool, or write a helper file and splice it in. (Four joined lines from earlier sessions were found and fixed.)
- Git Bash turns arguments starting with "/" into paths (`/freq` -> `C:/Program Files/Git/freq`): set
  `MSYS_NO_PATHCONV=1` when driving the REPL with "/" commands from bash, or drive it from a Python subprocess
  that writes lines to its stdin with short sleeps (that also works for the watcher's timed calls).
- Tests run with `world.AUTO_GENERATE = False`, `ATC_METAR=off` and a fake `ATC_LNM_DB` (conftest): no navdata FIRs
  and no sim db in normal tests. tests/test_navdb.py opts back in and runs only where the databases exist.
- `runway_in_use(..., use="departure"|"arrival")`: pass `use` at every new call site; without it the runway configs
  are ignored (the plain best-headwind runway).
