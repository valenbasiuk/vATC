# Next session plan (written 2026-10-06, end of session)

Start here after a `/clear`: read CLAUDE.md (auto-loaded), then this file, then work the steps in order.
State at the end of the session: 161 tests pass, 6/6 scenarios pass, everything imports. Valen committed up to
"METARs" (e2f0e35); everything after it is uncommitted on disk (`git status`): monitor.py, voices.py, pattern.py,
download_voices.py, chatter/FAA/descent fixes, tests/test_atis.py, tests/test_monitor.py. Don't commit unless Valen asks.

## 1. Finish the VFR circuit (`src/atc/pattern.py`): written, NOT wired in, no tests
What exists: `pattern.handle()` (inbound join / straight-in, downwind sequence, base/final clearance, touch and go,
VFR departure with turnout or circuits), `pattern.watcher_event()` (after a touch and go "report downwind"; a VFR
departure at 8 NM "frequency change approved"), `pattern.context_lines()`. Session fields already exist:
`in_circuit`, `circuit_intention`, `touched_down`, `zone_left`.
To do:
- `main.handle()`: call `pattern.handle(session, airport, facility, own, pilot_text, traffic, preferred)` just
  before `handle_flow(...)` (after `handle_taxi`); `plan_lines += pattern.context_lines(session)`.
- `main._Callbacks.tick()`: `pattern.watcher_event(self.session, self.world, own, traffic)` after
  `monitor.radar_event`. Add to it: in the circuit, on final <= 2.5 NM, not cleared, runway free -> "runway X,
  cleared to land" (or touch and go / FAA "cleared for the option") when the pilot didn't report final.
- `enroute.arrival_event()`: the "welcome, vacate via X" block sits after `if plan is None: return None`, so VFR
  flights never get it. Move it before that check; skip it when `circuit_intention == "touch and go"`.
- Watch out: `_POSITION` matches "downwind" inside readbacks (the "report" filter handles "report downwind"); the
  first-contact inbound check must stay before the position reports; IFR flights must not be affected (`is_vfr`).
- New tests/test_pattern.py: SABE (ICAO) and KSFO (FAA) inbound joins, straight-in when lined up, downwind number
  two behind an AI on final (`sim.add_on_final`), base -> cleared to land, touch and go -> airborne again ->
  "report left downwind", ready + "request left turnout", leaving the zone at 8 NM, the join readback is silent.
- `tools/compare_models.py`: its "VFR inbound SARC" turn becomes code-owned; swap in a turn the model still owns.
Done when: a whole circuit at SABE in the fake REPL (`--airport SABE`, `/freq 118.85`, `/air`, calls) makes no LLM call.

## 2. Area control for every Argentine FIR (bug: a SARC departure is handed to Ezeiza Control today)
`World.control()` takes the nearest `airspace/*.yaml` (by its reference lat/lon) within 700 NM, so with only
SAEF.yaml every Argentine departure goes to Ezeiza. Add one file per FIR, `kind: CTR`, `spoken: "<X> Control"`,
`trans_alt_ft: 3000`, `needs_review: false`, with this source comment (VATSIM Argentina operations manual v1.2.1,
Jan 2025, argentina.vatsur.org; same source as SAEF.yaml):
- `airspace/SARR.yaml` Resistencia FIR, ref SARE (-27.45, -59.06): SARR_CTR 124.300 "Resistencia Centro". Covers SARC.
- `airspace/SACF.yaml` Cordoba FIR, ref SACO (-31.31, -64.21): SACF_CTR 128.800, SACF_N_CTR 125.100, SACF_S_CTR 126.500.
- `airspace/SAMF.yaml` Mendoza FIR, ref SAME (-32.83, -68.79): SAMF_CTR 126.600.
- `airspace/SAVF.yaml` Comodoro Rivadavia FIR, ref SAVC (-45.79, -67.47): SAVF_CTR 126.750, SAVF_N_CTR 125.500,
  SAVF_S_CTR 125.700.
- Also from that manual (not used yet): SARE_APP 119.400 "Resistencia Control", SARE_TWR 118.700, SARE_GND 121.950,
  SARE_ATIS 127.850, SARC_TWR 118.300 "Corrientes Torre". The sim's db gives SARC APP 118.1 / 118.7 "Resistencia":
  ask Valen which one MSFS shows before changing SARC.yaml.
- Check the reference points: SABE -> SAAR must still go to Ezeiza (Rosario is in SAEF_N); SARC departures -> SARR.
- Tests: SARC departure -> "contact Resistencia Control one two four decimal three, good day".
Later (worldwide, no hand files): little_navmap_navigraph.sqlite has a `boundary` table (types FIR/UIR/C, name,
`com_frequency` in kHz like 124100, `geometry` blob, bounding boxes overlap) -> decode the blob, point-in-polygon,
"<name> Control"/"<name> Center". The data is AIRAC 1801 (2018): hand files win where they exist.

## 3. Things only the real sim can tell (Valen flies, brings the terminal output)
Ask Valen to run (voice, PTT, no --airport, no --simbrief first, then with it):
`python -m atc --sim --voice voices/en_US-libritts-high.onnx --ptt [--ptt-joy N --ptt-joy-device D]`
- Spawn at a random airport: is it found, is airports/<ICAO>.yaml written, does Ground answer? (`world.discover`)
- Callsign from the sim's ATC settings without a plan (`SimConnectSource.identity()`, from the user's own record in
  the AI list). Printed as "callsign from the sim: ...".
- ATIS on COM1, and on COM2 (`COM_RECEIVE:2` through Python-SimConnect may not exist: then COM2 is simply ignored).
  `ZULU_TIME`, `AMBIENT_TEMPERATURE` units.
- Voices by ear: accents (Argentine controllers = Spanish accent), speaking rate, hiss, squelch tail. If the Spanish
  accent is too hard to understand, make it optional (a `--accents` switch or per-country table in voices.py).
- Chatter: pushback detection (`tracker._backwards`) with real AI; "contact NorCal Departure".
- Radar monitoring in a real flight: false "check altitude" (e.g. climbing via SID with level-offs is fine: only
  overshoots count), traffic alerts too chatty or not, the go-around when an AI is still on the runway.
- `[timing]` lines: STT time after the faster-whisper changes (was ~2.7 s with small.en).
Fix what comes back before step 4.

## 4. Next realism items (pick in this order unless Valen asks otherwise)
1. FAA clearance initial altitude ("climb via SID" / "maintain five thousand"); hold short of crossed runways in
   taxi clearances (FAA "hold short runway 1L" is a readback item already).
2. Separate departure / arrival runways per airport (YAML hand fields `departure_runways`, `arrival_runways`;
   KSFO departs 1L/1R, lands 28L/28R; SABE uses one runway).
3. Approach/Departure chatter for AI arrivals and departures (the tracker sees them; Tower/Ground only today).
4. Conditional clearances ("behind the landing Airbus three twenty, line up and wait behind"), line up and wait for
   the user when traffic is on short final.
5. Speed control on the arrival ("reduce speed one eight zero knots"), holding when the runway is busy.
6. A tools/voice_samples.py that writes one WAV per accent pool so Valen can pick/blacklist voices.
7. Spanish phraseology + Spanish ATC voice (roadmap 20) once Valen wants it.

## Notes for whoever picks this up
- Python heredocs with `\b`/`\d` in replacement strings get mangled (Python processes the escapes): edit regex code
  with the Edit tool, not with `python - <<EOF` string replacement.
- Git Bash turns arguments starting with "/" into paths (`/freq` -> `C:/Program Files/Git/freq`): set
  `MSYS_NO_PATHCONV=1` when driving the REPL with "/" commands from bash.
- A test once wrote airports/SADF.yaml (deleted). Tests now run with `world.AUTO_GENERATE = False` and
  `ATC_METAR=off` (conftest); keep it that way.
- Sample WAVs of the accent voices from 2026-10-06 were in the session scratchpad (temporary); regenerate with
  `PiperTTS.synthesize(text, bank.pilot(...))` if needed.
