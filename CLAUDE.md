# CLAUDE.md — context for the IDE session

Read this first, then docs/ROADMAP.md (status per item, what's next) and docs/OPEN_QUESTIONS.md.
docs/PRE_IDE_CHECKLIST.md is the original setup checklist; most of it is done (see "Where we are").

## Where we are (2026-10-05)
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
- Next: real-sim check of the watcher/handoffs and AI
  type fields; `airspace/SAEF.yaml` frequencies; probe_taxi with the SDK DLL; STT/PTT (6-7); pattern sequencing (13); ATIS (14).
- Valen wants it as realistic as real ATC. He knows less phraseology than you: fix wrong phraseology without asking.
  Prefer moving decisions into code over "letting the model think" (latency, and errors come from missing facts).

## How Valen runs it
```powershell
python tools/probe_simbrief.py --username Valentino951      # fetch latest OFP -> simbrief_last.json
# provider-prefixed list, tried in order; keys are read from env vars or the Windows user environment (setx)
$env:ATC_LLM_MODEL = "gemini:gemini-flash-lite-latest,nvidia:nvidia/nemotron-3-super-120b-a12b,openrouter:nvidia/nemotron-3-super-120b-a12b:free,groq:qwen/qwen3.8-27b"
python -m atc --airport SABE --simbrief simbrief_last.json  # /freq 129.3 = Delivery, 121.9 Ground; fake sim starts on Ground
```
Old style still works (`ATC_LLM_BASE_URL` + `ATC_LLM_API_KEY` + unprefixed models). Keys: `GROQ_API_KEY`, `GEMINI_API_KEY`,
`CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` (client.py PROVIDERS).
Fake-sim REPL: `/wind 300 10 1015`, `/final 2 31`, `/onrwy`, `/notraffic`, `/air`, `/near SAAR 30 6000`, `/ground`.
Compare models: `python tools/compare_models.py [--only groq] [--runs N]` (scores the LLM-owned turns).
Piping input from Windows PowerShell 5.1 to python is buffered until the end and starts with a BOM; to test timers
(standby callback) drive the REPL from a Python subprocess script instead.

## What this is
Valen's personal AI ATC for MSFS 2024 (Microsoft Store version). Personal use only. It replaces paying for SayIntentions.AI and does not wait for BeyondATC's VFR. Standalone: it does NOT use OpenSquawk's hosted service (invite-based, wants API keys). OpenSquawk's Bridge app is public (AGPL-3.0) and uses the same `SimConnect` Python package, so it is a reference for reading the sim; check AGPL terms before copying code.

Loop: push-to-talk -> STT (faster-whisper, CPU) -> prompt builder -> small LLM -> TTS (Piper, local) -> radio filter.
Context fed to the LLM each turn: own telemetry (incl. wind, QNH) + nearby AI traffic (SimConnect) + computed runway in use + an airport YAML file.

## Goals that drive decisions
- Home airports: SABE (Aeroparque), SARC (Corrientes). Plus any random US airport set up in 10-15 min: `atc-gen KXXX`, check the YAML, fly.
- Latency budget 3-4 s from end of speech to start of audio. More is annoying.
- VFR at towered airports was the original focus. Valen also flies airliners IFR from SABE (SimBrief plans, e.g. MAR4133
  SABE-SAAR F100), so the IFR departure flow got built first; both matter now.
- Cheap: small model (Claude Haiku 4.5 target, roughly $6-8/month at 3 flights/day by my estimate). Free OpenRouter or Gemini API tiers while developing. LLM client is OpenAI-compatible, so switching is env vars only (`ATC_LLM_BASE_URL`, `ATC_LLM_API_KEY`, `ATC_LLM_MODEL`).
- Voices: Piper first (English), then Spanish and Norwegian (Valen practices Norwegian). Cloud voice only if Piper isn't enough.
- No LittleNavMap. VATSIM only as the optional "who covers me" helper (Phase 7, low priority; he wants either VATSIM or AI ATC, not a mix).
- Design rule: CODE decides facts (runway in use, who is on final, readback correctness), the LLM only phrases them. The LLM must never invent runways, frequencies, weather, traffic or taxiways.
- Fixed-form transmissions (clearance, readback corrections, "readback correct", push/start, taxi, takeoff/landing,
  check-ins, handoffs, traffic information, "say again your callsign") are produced entirely by code: exact, instant,
  testable. The LLM handles free-form turns (VFR pattern, questions) with facts in CONTEXT, then factcheck.py.

## Layout
```
src/atc/models.py        dataclasses (OwnState, Traffic, Airport, Runway, Frequency, Facility)
src/atc/geo.py           distance / bearing helpers
src/atc/runway.py        runway in use from wind (headwind, calm -> longest)
src/atc/facility.py      COM1 frequency -> which controller answers (None / ATIS / CTAF = silent)
src/atc/main.py          handle() = one transmission -> one reply (code-owned replies first, then LLM + fact check);
                         text/PTT loops; _Callbacks = watcher thread for controller-initiated calls (standby, handoffs)
src/atc/world.py         all airports of the plan + airspace/*.yaml; tuned frequency -> (airport, facility)
src/atc/flow.py          takeoff/landing detection, handoffs, check-ins, takeoff/landing clearances (code-owned)
src/atc/taxi.py          taxi graph (OSM / MSFS json) -> route -> taxi clearance (code-owned)
src/atc/traffic.py       traffic information from the pilot's view (code-owned), aircraft type names
src/atc/factcheck.py     rejects LLM replies stating numbers/types/taxiways not in their input
src/atc/flightplan.py    SimBrief OFP json -> FlightPlan, squawk assigned by code (stable per callsign)
src/atc/session.py       per-flight state: telephony ("Martinair", learned from the pilot's call), clearance
                         state none -> standby -> issued -> confirmed, pending correction items, positions contacted
src/atc/clearance.py     IFR clearance issued + readback-checked by code, standby callback, push/start, LLM context lines
src/atc/phrase.py        ICAO pronunciation (digits, niner, "decimal", flight levels, SID names, spoken callsign)
src/atc/readback.py      taxi/takeoff/landing readback check + is_acknowledgement() (silence on "roger"/correct readbacks)
src/atc/sequence.py      runway status (on runway / on final) -> Tower may or may not clear takeoff/landing; guard()
src/atc/scenarios.py     scripted scenario replay with content checks (python -m atc.scenarios)
src/atc/sim/             base.py (interface), fake.py (works), simconnect_source.py (own aircraft, UNTESTED),
                         ai_traffic.py (raw ctypes AI traffic, parsing tested, DLL calls UNTESTED)
src/atc/airports/        gen.py (OurAirports CSV -> YAML), schema.py (load/save/validate)
src/atc/llm/             prompt.py (where quality work happens), client.py (stub + OpenAI-compatible)
src/atc/audio/           tts.py (Piper Python API), stt.py, ptt.py (all UNTESTED), radio_fx.py (tested)
tools/                   check_env.py, probe_own.py, probe_traffic.py, probe_voice.py, probe_stt.py,
                         probe_simbrief.py (fetch OFP), compare_models.py (score LLM-owned turns),
                         fetch_osm_taxi.py (OSM taxiways -> airports/osm/), probe_taxi.py (MSFS taxi data, UNTESTED)
scenarios/               6 scenarios (YAML; `silence_ok` / `expect_silence` for turns ATC shouldn't answer)
airports/                SABE, SARC, SAAR, SAAV (generated; hand fields spoken_name, taxi_routes), KSFO; osm/ taxi maps
airspace/                SAEF.yaml (area control; frequencies TODO)
tests/                   97 tests; synthetic fixtures (KTST, SATS, SADX, KNTW are made up); test_clearance.py = IFR
                         departure, test_sequence.py = sequencing + audit fixes, test_flight.py = taxi/handoffs/etc.
```

## Status honesty (read before trusting anything)
Verified on Valen's PC: tests, own telemetry (squawk fixed), AI traffic, airport generation, Piper voice, OpenRouter LLM
calls, the SABE IFR departure flow in the text REPL (Delivery -> Ground -> taxi) with nemotron.
Not reported yet: faster-whisper STT and push-to-talk (`--ptt`), radio filter by ear, sentence-streamed Piper playback,
the telemetry watcher/handoffs with the real sim, AI type/airline fields, any VFR pattern work at SABE/SARC.
Fake sim only so far: the whole SABE -> SAAR flow (taxi, Tower, handoffs, arrival).
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
