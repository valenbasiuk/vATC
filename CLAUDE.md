# CLAUDE.md — context for the IDE session

Read this first, then docs/PRE_IDE_CHECKLIST.md (what Valen verifies on his PC), docs/ROADMAP.md and docs/OPEN_QUESTIONS.md.

## What this is
Valen's personal AI ATC for MSFS 2024 (Microsoft Store version). Personal use only. It replaces paying for SayIntentions.AI and does not wait for BeyondATC's VFR. Standalone: it does NOT use OpenSquawk's hosted service (invite-based, wants API keys). OpenSquawk's Bridge app is public (AGPL-3.0) and uses the same `SimConnect` Python package, so it is a reference for reading the sim; check AGPL terms before copying code.

Loop: push-to-talk -> STT (faster-whisper, CPU) -> prompt builder -> small LLM -> TTS (Piper, local) -> radio filter.
Context fed to the LLM each turn: own telemetry (incl. wind, QNH) + nearby AI traffic (SimConnect) + computed runway in use + an airport YAML file.

## Goals that drive decisions
- Home airports: SABE (Aeroparque), SARC (Corrientes). Plus any random US airport set up in 10-15 min: `atc-gen KXXX`, check the YAML, fly.
- Latency budget 3-4 s from end of speech to start of audio. More is annoying.
- VFR at towered airports is the focus. IFR is "fine already" in other tools.
- Cheap: small model (Claude Haiku 4.5 target, roughly $6-8/month at 3 flights/day by my estimate). Free OpenRouter or Gemini API tiers while developing. LLM client is OpenAI-compatible, so switching is env vars only (`ATC_LLM_BASE_URL`, `ATC_LLM_API_KEY`, `ATC_LLM_MODEL`).
- Voices: Piper first (English), then Spanish and Norwegian (Valen practices Norwegian). Cloud voice only if Piper isn't enough.
- No LittleNavMap. VATSIM only as the optional "who covers me" helper (Phase 7, low priority; he wants either VATSIM or AI ATC, not a mix).
- Design rule: CODE decides facts (runway in use, who is on final, readback correctness), the LLM only phrases them. The LLM must never invent runways, frequencies, weather or traffic.

## Layout
```
src/atc/models.py        dataclasses (OwnState, Traffic, Airport, Runway, Frequency, Facility)
src/atc/geo.py           distance / bearing helpers
src/atc/runway.py        runway in use from wind (headwind, calm -> longest)
src/atc/facility.py      COM1 frequency -> which controller answers (None / ATIS / CTAF = silent)
src/atc/main.py          handle() = one transmission -> one reply; text REPL around it
src/atc/flightplan.py    SimBrief OFP json -> FlightPlan, squawk assigned by code
src/atc/session.py       per-flight state: telephony ("Martinair"), clearance state
src/atc/clearance.py     IFR clearance issued + readback-checked by code, push/start
src/atc/phrase.py        ICAO pronunciation (digits, "decimal", flight levels, SID names)
src/atc/scenarios.py     scripted scenario replay with content checks (python -m atc.scenarios)
src/atc/sim/             base.py (interface), fake.py (works), simconnect_source.py (own aircraft, UNTESTED),
                         ai_traffic.py (raw ctypes AI traffic, parsing tested, DLL calls UNTESTED)
src/atc/airports/        gen.py (OurAirports CSV -> YAML), schema.py (load/save/validate)
src/atc/llm/             prompt.py (where quality work happens), client.py (stub + OpenAI-compatible)
src/atc/audio/           tts.py (Piper Python API), stt.py, ptt.py (all UNTESTED), radio_fx.py (tested)
tools/                   check_env.py, probe_own.py, probe_traffic.py, probe_voice.py, probe_stt.py
scenarios/               6 starter scenarios (YAML)
airports/                SARC.yaml, SABE.yaml seeds (approximate coords from memory, no runways/freqs)
tests/                   31 tests, synthetic fixtures (KTST, SATS, KNTW are made up)
```

## Status honesty (read before trusting anything)
Tested here (Linux sandbox, no MSFS): models, geo, runway logic, facility resolver, airport generator on synthetic CSVs, prompt builder incl. weather, stub LLM, scenario harness, text REPL, radio_fx, AI-traffic byte parsing. 31 tests pass.
Checked against public docs but never run: OurAirports column names and frequency types, Piper Python API, SimConnect C API signatures used in ai_traffic.py (from memory of the SDK, flagged VERIFY).
NOT run at all, needs Valen's PC: the real sim link (own aircraft + AI traffic), Piper audio, faster-whisper, push-to-talk, any real LLM call, real SARC/SABE data.
Every unverified spot is marked `VERIFY` or `TODO` in the code. Valen runs the probe scripts first and brings you their output; start from that, don't guess around it.

## Conventions
- Python 3.11+, 64-bit on Windows (SimConnect.dll won't load in 32-bit). `pip install -e .[dev,sim,audio]`.
- Run: `python -m atc --airport KTST --airports-dir tests/fixtures/airports` (text, fake sim, stub LLM). Real sim: add `--sim` (and `--dll PATH` if the bundled DLL fails).
- Tests: `pytest`. Scenarios: `python -m atc.scenarios`.
- Heavy imports (SimConnect, faster-whisper, sounddevice, piper) stay lazy so tests run anywhere.
- Never regenerate over hand-edited airport files; gen.py skips existing files unless `--force`.
- Unknown weather is `None`, never 0; the prompt tells the model not to state values that are absent.
- LLM output varies, so tests check content rules, never exact strings.
- API keys only in environment variables. Never commit them.
- Valen writes Spanish (Rioplatense) and English. Answer in the language he uses.
