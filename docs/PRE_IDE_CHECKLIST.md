# Before you open the IDE: do and check these yourself

> Status 2026-10-05: Parts A, B, C and D1 are done (model chosen, env, own data, AI traffic, airports, Piper voice).
> Still open: D2 (speech-to-text / push-to-talk). Part E is superseded by `tools/compare_models.py`. See docs/ROADMAP.md.

Everything here needs your PC, your MSFS or your accounts, so I could not do it for you.
Go in order. Each step says what "good" looks like and what to bring to the IDE session if it isn't.
Commands are for Windows PowerShell, run from the project folder.

Honest state of the code: the text loop, airport files, prompt, runway/wind logic, scenario tests
and byte parsing are tested (31 tests). The real sim link, AI traffic, Piper, speech-to-text and
push-to-talk are written from docs and have NEVER run. Steps 4-9 are where that gets settled.

---

## Part A. Decisions and accounts (no install needed, about 30 min)

**A1. Pick the model you will develop with. Don't pay yet.**
- Easiest: an OpenRouter account (openrouter.ai) with a key, using a model marked free (names end in `:free`; the list changes, so pick one from their models page).
- Alternative: a Google AI Studio key for the Gemini API. Check its current free limits and the OpenAI-compatible base URL in Google's docs.
- Later, when you pay: the $20 Claude plan is for Claude Code (the coding help). The ATC itself calls the API, billed separately in the Anthropic Console (a few dollars of credit). Through OpenRouter, Haiku 4.5 is `anthropic/claude-haiku-4.5`.
- Never paste an API key into a chat or commit it. Use environment variables only.

**A2. Look at OpenSquawk's Bridge for 10 minutes (optional but useful).**
- github.com/OpenSquawk/OpenSquawk-Bridge is public (AGPL-3.0) and can be run from source. The invite/API-key part you saw belongs to their hosted service, not to the Bridge code.
- It uses the same `SimConnect` Python package as this project, so its `msfs_source.py` is a working reference for reading the sim. Don't copy code into this project without checking the AGPL terms; reading it for ideas is fine.

## Part B. Install and sanity-check (about 30-45 min)

**B1. Python.** Install 3.11 or newer, 64-bit, from python.org (tick "Add to PATH").
Check: `python --version` and `python -c "import struct; print(struct.calcsize('P')*8)"` must print 64.

**B2. Project.**
```
cd path\to\atc-ia
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev,sim]
```
If PowerShell refuses to activate the venv, run `Set-ExecutionPolicy -Scope Process RemoteSigned` once in that window and retry.
Add audio separately so a failure there doesn't block the rest: `pip install -e .[audio]`.
If `faster-whisper` or `piper-tts` won't install, note the exact error for the IDE session.

**B3. Tests and text mode.**
```
pytest                         # expect: 31 passed
python tools/check_env.py      # expect: no [FAIL] lines
python -m atc.scenarios        # expect: 6/6 scenarios passed (stub model)
python -m atc --airport KTST --airports-dir tests/fixtures/airports
```
In the text loop try: `/freq 118.1`, `/wind 270 10 1013`, `ready for departure`. Expect "Runway 27, cleared for takeoff. (stub)".
Bring to the IDE if not: the full error text.

## Part C. Checks that need MSFS (about 1-2 hours)

**C1. Own aircraft data.** Start MSFS, load a flight, wait for the cockpit. Then:
```
python tools/probe_own.py
```
Compare each value with the sim:
- Position and MSL altitude plausible; AGL is about 0 on the ground.
- Heading: the sim gives TRUE heading, your compass shows MAGNETIC. A difference of several degrees in Argentina is normal.
- COM1: tune a frequency in the cockpit and watch it change. Try one 8.33 kHz channel (like 118.105) and note what prints.
- Squawk: set 1234 in the cockpit. It must print `1234`, not a strange number like 4660 (that would mean BCD encoding).
- Wind and QNH: compare with the sim's weather. They print as "unknown" if the read failed, which is fine and safe.
If it fails: try `--dll "path\to\SimConnect.dll"` using the DLL from the MSFS 2024 SDK. Bring the full output either way.

**C2. AI traffic. This is the biggest unknown in the project.**
First, a 2-minute check of constants I could not confirm online: open `.venv\Lib\site-packages\SimConnect\Enum.py` and look at `SIMCONNECT_RECV_ID` (the members are numbered by order: the 3rd is OPEN=2, `SIMOBJECT_DATA` should be 8 and `SIMOBJECT_DATA_BYTYPE` 9) and `SIMCONNECT_DATATYPE` (`STRING32` should be 6). If any differ, change the numbers at the top of `src/atc/sim/ai_traffic.py`.
Then, in MSFS settings, turn AI traffic up. Spawn at a busy default airport, then:
```
python tools/probe_traffic.py
```
Good: a list of aircraft with callsigns, distances, altitudes and ground/air flags matching what you see outside.
Note: is your own aircraft in the list (it prints "looks like YOU")? Do online/multiplayer aircraft show up? Do callsigns look real?
If it errors or prints 0 aircraft while you can see traffic, bring the whole output. Don't try to fix it alone.

**C3. Airport data.**
```
atc-gen --download
atc-gen KSFO            # or any US airport you like
```
Open `airports\KSFO.yaml`. Check: runway idents and headings are right, frequencies are present, and their kinds look sensible (TWR, GND, ATIS, ARR, DEP...), `towered` is correct.
Then your home airports (copy the `notes:` lines from the seed files first, because `--force` overwrites):
```
atc-gen SARC SABE --force
```
Check what OurAirports actually has for Argentina. Missing frequencies or runways are likely. Fill them from the AIP charts (ANAC/EANA) or from the sim's own airport info, and set `pattern_alt_agl_ft` and `pattern_direction` for each runway by hand.
Time it: ICAO to a usable file should take under 10-15 minutes including your edits.

## Part D. Voice (about 1 hour, no sim needed)

**D1. Piper voices.**
```
python -m piper.download_voices en_US-lessac-medium --data-dir voices
python tools/probe_voice.py voices\en_US-lessac-medium.onnx
```
Listen with and without the radio effect. Try 3 or 4 different voices (the voice list is shown by `python -m piper.download_voices` with no argument) and note your favorite. Try `--speed 0.85`.
Also look at how many seconds each synthesis takes (it prints). Good: well under 1 s per sentence after the first one.

**D2. Speech-to-text.**
```
python tools/probe_stt.py
```
Hold F9, say a radio call, release. Check that callsigns and runway numbers come out right and the time printed is about 1 s or less. Try `--model base.en` if `small.en` is too slow.

## Part E. The model (about 30 min, after A1)

```
$env:ATC_LLM_BASE_URL = "https://openrouter.ai/api/v1"
$env:ATC_LLM_API_KEY  = "your key"
$env:ATC_LLM_MODEL    = "the model name"
python -m atc.scenarios
python -m atc --airport KTST --airports-dir tests/fixtures/airports
```
Check: how many of the 6 scenarios pass, and whether replies feel instant, a few seconds, or painfully slow. Free models may be slow or rate-limited at busy hours, and that is useful to know before you rely on one.

## Part F. Ready for the IDE when

- [ ] `pytest` passes and `check_env.py` shows no FAIL
- [ ] `probe_own.py` values match the sim (or you have the exact failures)
- [ ] `probe_traffic.py` shows real traffic (or you have the exact output)
- [ ] `KSFO.yaml` (or similar) generated and looks right
- [ ] SARC and SABE files have real runways and frequencies, or you know what is missing
- [ ] You chose a Piper voice, and you know STT speed
- [ ] You have a model that answers, and its scenario score

Bring to the IDE session: the output of `check_env.py`, `probe_own.py` and `probe_traffic.py`, the chosen voice and model names, and anything that failed.
Then start at docs/ROADMAP.md item A2 (or wherever the checks above say).
