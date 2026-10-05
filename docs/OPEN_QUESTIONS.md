# Open questions

Marked OPEN (needs the PC/sim to settle), CHECKED (verified against public docs, not by running),
or DONE. Items point to the step in docs/PRE_IDE_CHECKLIST.md that settles them.

## OPEN

1. **AI traffic through raw SimConnect (checklist C2).** `src/atc/sim/ai_traffic.py` calls the public C API through ctypes. Unknown until run: whether the constants and struct layout are right on MSFS 2024, whether the user's own aircraft appears in the list, whether online/multiplayer aircraft appear. A forum thread says multiplayer traffic is partly returned and distinguishable by very large object ids, with limited data, so don't rely on it. Constants: AIRCRAFT=2, INT32=1, FLOAT64=4, STRING8=5 are confirmed from the Python package's Enum.py; STRING32=6 and the recv ids (EXCEPTION=1, OPEN=2, QUIT=3, SIMOBJECT_DATA=8, SIMOBJECT_DATA_BYTYPE=9) are from memory and unconfirmed (the online SDK docs could not be fetched). Fallbacks: the Go library (Zwergpro/simconnect-go), or the MSFS SDK samples.
2. **Simvar details (C1).** Indexed names (`COM_ACTIVE_FREQUENCY:1`, `TRANSPONDER_CODE:1`), heading units (radians vs degrees), transponder encoding (BCD?), units of `SEA_LEVEL_PRESSURE`, whether wind is read at the aircraft or at the airport.
3. **Which SimConnect.dll for the Store version (C1).** The PyPI package bundles one (the OpenSquawk Bridge uses the same package for MSFS 2020/2024). If it fails, use the DLL from the MSFS 2024 SDK via `--dll` or `ATC_SIMCONNECT_DLL`.
4. **Real OurAirports data for Argentina (C3).** Frequencies and runways for SARC/SABE may be missing or outdated; manual pass from the AIP needed. Seeds only have approximate coordinates from memory.
5. **8.33 kHz channels (C1).** Frequency matching has a 5 kHz tolerance; check how MSFS reports 8.33 vs 25 kHz spacing, especially for Argentine airports.
6. **Piper (D1)** and **speech-to-text / push-to-talk (D2).** Written from docs, never run. `pynput` key names, `sounddevice` device selection, and Whisper CPU speed need real tests.
7. **Latency (E).** No measurement exists yet for any stage.
8. **Prompt caching** minimum size for Haiku; the prompt is small and may be under it.
9. **Gemini API free tier** terms and limits, and its OpenAI-compatible base URL (A1).
10. **Magnetic vs true.** Winds and headings are TRUE in the sim, controllers speak MAGNETIC. Runway choice is fine; spoken wind/heading values will be a few degrees off in Argentina. Decide later whether to apply magnetic variation.
11. **BeyondATC "replace voices with a local model" claim.** Not confirmed from public pages (their offline voices are already a local model; I found nothing about swapping in your own). Ask the person who said it.

## CHECKED against public docs (not run)

- OurAirports file columns and frequency types match `gen.py` (data dictionary at ourairports.com/help/data-dictionary.html). Frequency types are TWR, GND, RMP, ATIS, ARR, DEP, ATF, CTAF, UNICOM, RCO, RDO. An earlier draft assumed `APP`; `ARR` is the real type (fixed in `facility.py`).
- Piper changed: it is now `OHF-Voice/piper1-gpl`, CLI is `python -m piper -m <voice> -f out.wav -- 'text'`, Python API is `PiperVoice.load(path)` then `voice.synthesize(text)` yielding chunks. An earlier draft used the old CLI flags; `audio/tts.py` now uses the Python API and keeps the voice loaded.
- The Python `SimConnect` package only requests the USER aircraft (its `request_data` hardcodes that type), so AI traffic needs the raw calls above.
- OpenSquawk's Bridge is public (AGPL-3.0), runs from source, and uses the `SimConnect` package. The invite/API-key issue concerns the hosted service.

## DONE in the skeleton

- Wind, altimeter and runway-in-use are decided in code and given to the model as facts (`runway.py`, `llm/prompt.py`).
- Scenario replay harness with 6 scenarios (`python -m atc.scenarios`).
- Probe scripts for the PC (`tools/`).
