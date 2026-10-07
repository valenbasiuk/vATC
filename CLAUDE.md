# CLAUDE.md — context for the IDE session

Read this first, then **docs/NEXT_SESSION.md (the plan to continue with: work its steps in order)**, then
docs/ROADMAP.md (status per item) and docs/OPEN_QUESTIONS.md.
docs/PRE_IDE_CHECKLIST.md is the original setup checklist; most of it is done (see "Where we are").

## Where we are (2026-10-06, second session)
- Second session 2026-10-06 (worked docs/NEXT_SESSION.md steps 1, 2, 4.1-4.6): VFR circuit wired in (a whole circuit
  at SABE makes no LLM call; zone transits stay with the model); one airspace file per Argentine FIR (SARC departures
  -> Resistencia Control) + worldwide Center from the Navigraph FIR boundaries + Center->Center handoffs; FAA
  "maintain five thousand" without a SID; runway crossings ("hold short of runway 28R" -> "cross runway 28R");
  KSFO runway thresholds from the sim's db (were missing); separate departure/arrival runways (`runway_configs`,
  KSFO 1L/1R + 28L/28R); Approach/Departure chatter for AI; ICAO conditional line-up + automatic takeoff clearance
  once the runway is free; speed control on the arrival; tools/voice_samples.py + voices/blacklist.txt.
  Then (Valen couldn't fly): ground conflicts ("give way to the Airbus from the left", "hold position" -> "continue
  taxi"), approach names with chart suffix ("ILS Zulu") and FAA order, STAR from SimBrief ("descend via the SERFR
  four arrival"), progressive taxi ("I'll call your turns" -> "turn left on Delta"), VFR flight following /
  flight information service (squawk -> "radar contact, 8 miles south of San Francisco" -> Tower or "radar service
  terminated"). Then: runway requests ("request runway 13" -> approved/unable, one place decides the runway:
  `runway.session_runway`), "say again" word for word, radio/time checks and QNH/wind questions by code (`info.py`),
  holding on request (`holding.py`, Navigraph published holds), own navigation (no vectors), line up and wait behind
  a rolling departure, VFR "departure to the north", STT compound words ("take off"), one SimConnect AI request per
  tick (cache), a whole SABE -> SAAR flight test, PILOT STATE lines for the model. 230 tests, 6/6 scenarios.
  All fake-sim / sim-db only: docs/ROADMAP.md "WATCH IN THE SIM" lists the possible in-sim issues per feature.
- Session 2026-10-06 (Valen: "polish everything, as realistic as SayIntentions"): airports load themselves (spawn
  anywhere, no --airport with --sim; YAML written once from OurAirports or the sim's db), US/FAA phraseology (group
  callsigns "United four thirty-six", altitudes by transition altitude, "climb and maintain", "then as filed", "ground
  point eight"), ATIS from the sim + live METAR (aviationweather.gov) with the information-letter check, radar
  monitoring (level bust, 7700, traffic alerts, Tower go-around, "going around", mayday/pan pan, wrong handoff
  frequency), accent voices (l2arctic + vctk downloaded: every AI pilot and every controller position has its own
  voice by country), chatter polish (pushback, "NorCal Departure"), descent in two steps (Center FL100, Approach the
  final altitude). KSFO -> KLAX flown in the fake sim with the real sim data. 161 tests, 6/6 scenarios.
- Real sim, 2026-10-06: KSFO Ground answers after the auto-airport fix; AI chatter uses the sim's airline + flight
  number ("Speedbird two eight six"). Everything else from this session is fake-sim only (see Status honesty).

## Before (2026-10-05)
- Working on Valen's PC: env + tests, own telemetry, AI traffic, airport generation (SABE/SARC/SAAR), Piper voice
  (`voices/en_US-libritts-high.onnx`), real LLM calls through OpenRouter.
- Model: OpenRouter free `nvidia/nemotron-3-ultra-550b-a55b:free` (Valen's main) / `nvidia/nemotron-3-super-120b-a12b:free`
  (fastest, ~0.9 s). Avoid nemotron-3.5-lightning (invents taxiways) and gemma (always 429). Ollama was tried and dropped
  (sim stutter), uninstalled. Valen has only an OpenRouter key (`OPENROUTER_API_KEY`, user env var).
- IFR departure at the origin is done by CODE: SimBrief plan -> clearance, readback check and corrections, optional
  "standby" + callback, push/start, silence on correct readbacks. ICAO phraseology (Argentina).
- 2026-10-05 audit + build-out (flown as a pilot in the fake sim): the whole IFR flight SABE -> SAAR is now code-owned
  in one run: clearance, push, taxi with real taxiway names (OSM), takeoff/landing with sequencing, telemetry-driven
  handoffs, check-ins, traffic information. The LLM only gets free-form turns (VFR pattern work, questions), and every
  LLM reply is fact-checked. OpenRouter free tier = 50 requests/day total; Valen won't pay (Argentine card tax), so
  the plan is stacked free providers (groq/gemini/cerebras/...) via provider prefixes in ATC_LLM_MODEL.
- Keys set (user env, 2026-10-05): GROQ_API_KEY, GEMINI_API_KEY, NVIDIA_API_KEY, OPENROUTER_API_KEY. Model names
  move fast (2.5 Gemini and NVIDIA llama-3.3 are gone): `python tools/list_models.py` shows what exists.
- Valen wants it as realistic as real ATC. He knows less phraseology than you: fix wrong phraseology without asking.
  Prefer moving decisions into code over "letting the model think" (latency, and errors come from missing facts).

## How Valen runs it
```powershell
python tools/probe_simbrief.py --username Valentino951      # fetch latest OFP -> simbrief_last.json
# provider-prefixed list, tried in order; keys are read from env vars or the Windows user environment (setx)
$env:ATC_LLM_MODEL = "gemini:gemini-flash-lite-latest,nvidia:nvidia/nemotron-3-super-120b-a12b,openrouter:nvidia/nemotron-3-super-120b-a12b:free,groq:qwen/qwen3.8-27b"
python -m atc --airport SABE --simbrief simbrief_last.json  # /freq 129.3 = Delivery, 121.9 Ground; fake sim starts on Ground
```
In the sim `--airport` can be left out: the airport you are on is found (and its YAML written the first time).
Own traffic (departures + arrivals): add `--own-traffic [--own-factor 1.5] [--own-max N]`, sim AI traffic and
parked aircraft OFF; REPL `/owndep [ARG B738]`, `/ownarr [ga]`. Env: ATC_OWN_MODELS=fsltl, ATC_OWN_TUG=off|<title>,
ATC_OWN_PITCH_SIGN, ATC_OWN_TUG_YAW. `tools/probe_inject.py` = the injection probe (passed in MSFS 2026-10-07).
High Piper voices: `python tools/download_voices.py --high` (done 2026-10-07: cori, lessac, ljspeech, ryan + es).
Accent voices: `python tools/download_voices.py` once (done on Valen's PC 2026-10-06; `--list` shows the pools).
Preset (not live) weather in the sim: `$env:ATC_METAR = "off"` so the ATIS doesn't read the real METAR.
Voice in the sim: add `--sim --voice voices/en_US-libritts-high.onnx --ptt` (keyboard F9, or `--ptt-key f10`,
or a yoke button `--ptt-joy N --ptt-joy-device D` found with `python tools/probe_ptt.py`; on Valen's PC device 2
reports buttons 19 and 32 as always held: switches, don't use). `--mic` / `--audio-out` take a sounddevice index
(defaults: Blue Snowball mic, Samsung USB-C earphones). AI chatter is always on (Tower/Ground of the airport you're at).
Old style still works (`ATC_LLM_BASE_URL` + `ATC_LLM_API_KEY` + unprefixed models). Keys: `GROQ_API_KEY`, `GEMINI_API_KEY`,
`CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` (client.py PROVIDERS).
Fake-sim REPL: `/wind 300 10 1015`, `/final 2 31`, `/onrwy`, `/notraffic`, `/air`, `/near SAAR 30 6000`, `/ground`,
`/leg downwind|base|final 2|upwind|out` (own aircraft in the circuit), `/aidep`, `/aiarr 20` (AI with Approach chatter).
Voices: `python tools/voice_samples.py [--accent Spanish]` -> voice_samples/; bad ones into voices/blacklist.txt.
Compare models: `python tools/compare_models.py [--only groq] [--runs N]` (scores the LLM-owned turns).
Piping input from Windows PowerShell 5.1 to python is buffered until the end and starts with a BOM; to test timers
(standby callback) drive the REPL from a Python subprocess script instead.

## What this is
Valen's personal AI ATC for MSFS 2024 (Microsoft Store version). Personal use only. It replaces paying for SayIntentions.AI and does not wait for BeyondATC's VFR. Standalone: it does NOT use OpenSquawk's hosted service (invite-based, wants API keys). OpenSquawk's Bridge app is public (AGPL-3.0) and uses the same `SimConnect` Python package, so it is a reference for reading the sim; check AGPL terms before copying code.

Loop: push-to-talk -> STT (faster-whisper, CPU) -> prompt builder -> small LLM -> TTS (Piper, local) -> radio filter.
Context fed to the LLM each turn: own telemetry (incl. wind, QNH) + nearby AI traffic (SimConnect) + computed runway in use + an airport YAML file.

## Goals that drive decisions
- Home airports: SABE (Aeroparque), SARC (Corrientes). Plus any random airport with no setup: spawn there and its
  YAML is written by itself (2026-10-06); hand-check it later (`needs_review`). `atc-gen KXXX` still works.
- Latency budget 3-4 s from end of speech to start of audio. More is annoying.
- VFR at towered airports was the original focus. Valen also flies airliners IFR from SABE (SimBrief plans, e.g. MAR4133
  SABE-SAAR F100), so the IFR departure flow got built first; both matter now.
- Cheap: small model (Claude Haiku 4.5 target, roughly $6-8/month at 3 flights/day by my estimate). Free OpenRouter or Gemini API tiers while developing. LLM client is OpenAI-compatible, so switching is env vars only (`ATC_LLM_BASE_URL`, `ATC_LLM_API_KEY`, `ATC_LLM_MODEL`).
- Voices: Piper first (English), then Spanish and Norwegian (Valen practices Norwegian). Cloud voice only if Piper isn't enough.
- Little Navmap's scenery database is READ (navdb.py: taxiways, frequencies, magvar, approaches, airports; Valen's
  idea), the app itself isn't needed while flying. VATSIM data only as a source for frequencies (manuals), and the
  optional "who covers me" helper (Phase 7, low priority; he wants either VATSIM or AI ATC, not a mix).
- Design rule: CODE decides facts (runway in use, who is on final, readback correctness), the LLM only phrases them. The LLM must never invent runways, frequencies, weather, traffic or taxiways.
- Fixed-form transmissions (clearance, readback corrections, "readback correct", push/start, taxi, takeoff/landing,
  check-ins, handoffs, traffic information, "say again your callsign") are produced entirely by code: exact, instant,
  testable. The LLM handles free-form turns (VFR pattern, questions) with facts in CONTEXT, then factcheck.py.

## Layout
```
src/atc/models.py        dataclasses (OwnState, Traffic, Airport, Runway, Frequency, Facility)
src/atc/geo.py           distance / bearing helpers
src/atc/runway.py        runway in use from wind (headwind, calm -> longest); use="departure"/"arrival" with the
                         YAML's runway_configs (KSFO lands 28s, departs 1s)
src/atc/facility.py      COM1 frequency -> which controller answers (None / ATIS / CTAF = silent)
src/atc/main.py          handle() = one transmission -> one reply (code-owned replies first, then LLM + fact check);
                         text/PTT loops; _Callbacks = watcher thread for controller-initiated calls (standby, handoffs)
src/atc/world.py         all airports of the plan + airspace/*.yaml; tuned frequency -> (airport, facility);
                         discover(): loads/writes the airport you spawned at or tuned; control(): Center near you
                         (airspace file in reach, else the navdata FIR at your position: navdb.fir_at)
src/atc/atis.py          ATIS text (ICAO/FAA) + letter state + "information X is now current" check
src/atc/weather.py       METAR from aviationweather.gov (cached, background thread; ATC_METAR=off)
src/atc/ground.py        taxi conflicts with moving AI: give way / hold position / continue taxi (watcher)
src/atc/following.py     VFR flight following: squawk, "radar contact, <position>", termination near the field
src/atc/holding.py       holds on request (published ones from the Navigraph db), EFC, leaving the hold
src/atc/info.py          radio check, time check, "say QNH / wind" answered by code
src/atc/monitor.py       radar watch: level bust, 7700, traffic alerts, go-around; pilot mayday/pan pan/going around
src/atc/pattern.py       VFR circuit at towered fields: join/straight-in, sequence, touch and go, turn-outs, zone exit
src/atc/voices.py        voice bank: accent pools from voices/*.onnx; pilot voice by country, one per ATC position
src/atc/flow.py          takeoff/landing detection, handoffs, check-ins, takeoff/landing clearances (code-owned)
src/atc/taxi.py          taxi graph (OSM / MSFS json) -> route -> taxi clearance (code-owned); runway crossings
                         ("hold short of runway X" -> "cross runway X")
src/atc/traffic.py       traffic information from the pilot's view (code-owned), aircraft type names
src/atc/factcheck.py     rejects LLM replies stating numbers/types/taxiways not in their input
src/atc/enroute.py       radar work: directs, climb, descent at TOD, vectors, approach clearance, landing, vacate
src/atc/navdb.py         Little Navmap MSFS 2024 db (read-only): taxi map, frequencies, magvar, approaches, fixes
src/atc/tracker.py       AI traffic -> events (taxi out, line up, takeoff roll, departed, final, vacated, taxi in)
src/atc/chatter.py       ATC <-> AI exchanges for those events + RadioBus (turn-taking, gaps, stale, PTT)
src/atc/flightplan.py    SimBrief OFP json -> FlightPlan, squawk assigned by code (stable per callsign)
src/atc/session.py       per-flight state: telephony ("Martinair", learned from the pilot's call), clearance
                         state none -> standby -> issued -> confirmed, pending correction items, positions contacted
src/atc/clearance.py     IFR clearance issued + readback-checked by code, standby callback, push/start, LLM context lines
src/atc/phrase.py        ICAO/FAA pronunciation (digits, niner, "decimal"/"point", levels by transition altitude,
                         climb/descend forms, FAA group-form callsigns, SID names, wind rounded to 10 degrees)
src/atc/readback.py      taxi/takeoff/landing readback check + is_acknowledgement() (silence on "roger"/correct readbacks)
src/atc/sequence.py      runway status (on runway / on final) -> Tower may or may not clear takeoff/landing; guard()
src/atc/scenarios.py     scripted scenario replay with content checks (python -m atc.scenarios)
src/atc/own/             OUR OWN TRAFFIC (--own-traffic, docs/OWN_TRAFFIC_PLAN.md): catalog.py (FS Traffic / FSLTL
                         titles, GSX tug), schedule.py (FS Traffic schedules, movements per hour per airport),
                         airport.py (stands, push/taxi/line-up paths, exits + taxi-in), motion.py (kinematics:
                         taxi controller, takeoff, approach/landing), pilot.py (Departure/ArrivalPilot state
                         machines, stop behind traffic), injector.py (SimConnect create/release/freeze/set position
                         per sim frame, pause, gear heights; FakeInjector), manager.py (read + move threads, spawning,
                         clearances and radio for our aircraft); __init__ = registry merged into every traffic list
                         (own.merge), ignored by the tracker
src/atc/sim/             base.py (interface), fake.py (works), simconnect_source.py (own aircraft, UNTESTED),
                         ai_traffic.py (raw ctypes AI traffic, parsing tested, DLL calls UNTESTED)
src/atc/airports/        gen.py (OurAirports CSV -> YAML), schema.py (load/save/validate)
src/atc/llm/             prompt.py (where quality work happens), client.py (stub + OpenAI-compatible)
src/atc/audio/           tts.py (Piper; voice bank, models cached + preloaded), stt.py, ptt.py (keyboard + joystick),
                         joystick.py (WinMM buttons), radio_fx.py (band-pass, hiss, squelch tail)
tools/                   check_env.py, probe_own.py, probe_traffic.py, probe_voice.py, probe_stt.py, probe_ptt.py,
                         probe_simbrief.py (fetch OFP), compare_models.py (score LLM-owned turns),
                         download_voices.py (accent voices -> voices/), voice_samples.py (one WAV per voice, for
                         voices/blacklist.txt), list_models.py,
                         fetch_osm_taxi.py (OSM taxiways -> airports/osm/), probe_taxi.py (MSFS taxi data, UNTESTED)
scenarios/               6 scenarios (YAML; `silence_ok` / `expect_silence` for turns ATC shouldn't answer)
airports/                SABE, SARC, SAAR, SAAV, KSFO (+ any the world writes when you spawn/tune there); osm/ maps
airspace/                one per Argentine FIR: SAEF Ezeiza, SARR Resistencia, SACF Cordoba, SAMF Mendoza, SAVF
                         Comodoro Rivadavia (VATSIM Argentina manual); elsewhere the navdata FIRs (AIRAC 1801)
tests/                   230 tests (test_whole_flight.py: SABE -> SAAR end to end, zero LLM calls); synthetic fixtures (KTST, SATS, SADX, KNTW are made up); conftest keeps tests off
                         the LNM db, off the network (ATC_METAR=off) and from writing airport files (AUTO_GENERATE)
```

## Status honesty (read before trusting anything)
Verified on Valen's PC: tests, own telemetry (squawk fixed), AI traffic, airport generation, Piper voice, OpenRouter LLM
calls, the SABE IFR departure flow in the text REPL (Delivery -> Ground -> taxi) with nemotron.
Real sim (Valen, 2026-10-05/06): taxi at SABE, Ground -> Tower -> Approach handoffs, squawk handling, AI airline +
flight number in chatter, KSFO Ground after the auto-airport fix. 2026-10-07: IFR departure at SABE (Delivery,
push, taxi), ground conflicts "give way", aircraft injection (tools/probe_inject.py).
Not reported yet: faster-whisper STT and push-to-talk (`--ptt`), radio filter by ear, sentence-streamed Piper playback,
accent voices by ear, ATIS by ear (COM2 receive simvar unverified), callsign from the sim's ATC settings, spawn
detection writing a new YAML, radar monitoring/go-around in a real flight, any VFR pattern work at SABE/SARC,
AIRSPEED_INDICATED (speed control), runway crossings / runway configs / FIR Center handoffs in a real flight,
Approach/Departure chatter with real AI.
Fake sim only so far: the whole SABE -> SAAR flow (taxi, Tower, handoffs, arrival), KSFO -> KLAX (FAA), VFR circuits.
Unverified spots are marked `VERIFY` or `TODO` in the code. Valen runs the probe scripts and brings you their output;
start from that, don't guess around it.

## Conventions
- Python 3.11+, 64-bit on Windows (SimConnect.dll won't load in 32-bit). `pip install -e .[dev,sim,audio]`.
- Run: `python -m atc --airport KTST --airports-dir tests/fixtures/airports` (text, fake sim, stub LLM). Real sim: add `--sim` (and `--dll PATH` if the bundled DLL fails).
- Tests: `pytest`. Scenarios: `python -m atc.scenarios`.
- Heavy imports (SimConnect, faster-whisper, sounddevice, piper) stay lazy so tests run anywhere.
- Never regenerate over hand-edited airport files; gen.py skips existing files unless `--force`.
- Airport YAML hand fields: `spoken_name` (station names "Aeroparque Ground" and clearance limits "cleared to Rosario"),
  `taxi_routes` (runway -> "via ..."; Ground names taxiways only from here).
- Code-owned replies are tested with exact strings (tests/test_clearance.py); LLM replies with content rules only.
- Unknown weather is `None`, never 0; the prompt tells the model not to state values that are absent.
- LLM output varies, so tests check content rules, never exact strings.
- API keys only in environment variables. Never commit them.
- Valen writes Spanish (Rioplatense) and English. Answer in the language he uses.
