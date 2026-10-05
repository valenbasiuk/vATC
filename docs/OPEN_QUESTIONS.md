# Open questions

Marked OPEN (needs the PC/sim to settle), CHECKED (verified against public docs, not by running),
or DONE. Items point to the step in docs/PRE_IDE_CHECKLIST.md that settles them.

## OPEN

2. **Simvar details (C1).** Indexed names (`COM_ACTIVE_FREQUENCY:1`, `TRANSPONDER_CODE:1`), heading units (radians vs degrees), transponder encoding (BCD?), units of `SEA_LEVEL_PRESSURE`, whether wind is read at the aircraft or at the airport.
3. **Which SimConnect.dll for the Store version (C1).** The PyPI package bundles one (the OpenSquawk Bridge uses the same package for MSFS 2020/2024). If it fails, use the DLL from the MSFS 2024 SDK via `--dll` or `ATC_SIMCONNECT_DLL`.
5. **8.33 kHz channels (C1).** Frequency matching has a 5 kHz tolerance; check how MSFS reports 8.33 vs 25 kHz spacing, especially for Argentine airports.
6. **Piper (D1)** and **speech-to-text / push-to-talk (D2).** Written from docs, never run. `pynput` key names, `sounddevice` device selection, and Whisper CPU speed need real tests.
7. **Latency (E).** No measurement exists yet for any stage.
8. **Prompt caching** minimum size for Haiku; the prompt is small and may be under it.
9. **Gemini / NVIDIA free tiers.** Base URLs are in `tools/compare_models.py` and the client handles Gemini's `reasoning_effort` (it rejects OpenRouter's `reasoning` field with HTTP 400). Never run with a real key: Valen only has an OpenRouter key so far.
12. **SABE departure frequency.** The clearance says "departure frequency" = the DEP entry, else APP (SABE: APP 120.6). Check the real Aeroparque departure frequency in the AIP.
13. **Clearance wording at Aeroparque.** Current template is generic ICAO ("climb via SID, expect FL ten minutes after departure"). Real Argentine Delivery may give a different initial climb or omit the expected level; adjust `clearance.items()` if Valen knows the local form.
14. **Taxiway data source.** Hand-written `taxi_routes` vs reading MSFS facility data through SimConnect (ROADMAP 9b). Unknown: whether the ctypes approach in `ai_traffic.py` extends easily to `SimConnect_RequestFacilityData` on MSFS 2024.
15. **ATIS letter.** Pilots report "information Romeo"; nothing checks it yet because there is no ATIS (ROADMAP 14).
10. **Magnetic vs true.** Winds and headings are TRUE in the sim, controllers speak MAGNETIC. Runway choice is fine; spoken wind/heading values will be a few degrees off in Argentina. Decide later whether to apply magnetic variation.
11. **BeyondATC "replace voices with a local model" claim.** Not confirmed from public pages (their offline voices are already a local model; I found nothing about swapping in your own). Ask the person who said it.

## CHECKED against public docs (not run)

- OurAirports file columns and frequency types match `gen.py` (data dictionary at ourairports.com/help/data-dictionary.html). Frequency types are TWR, GND, RMP, ATIS, ARR, DEP, ATF, CTAF, UNICOM, RCO, RDO. An earlier draft assumed `APP`; `ARR` is the real type (fixed in `facility.py`).
- Piper changed: it is now `OHF-Voice/piper1-gpl`, CLI is `python -m piper -m <voice> -f out.wav -- 'text'`, Python API is `PiperVoice.load(path)` then `voice.synthesize(text)` yielding chunks. An earlier draft used the old CLI flags; `audio/tts.py` now uses the Python API and keeps the voice loaded.
- The Python `SimConnect` package only requests the USER aircraft (its `request_data` hardcodes that type), so AI traffic needs the raw calls above.
- OpenSquawk's Bridge is public (AGPL-3.0), runs from source, and uses the `SimConnect` package. The invite/API-key issue concerns the hosted service.

## DONE

- **AI traffic through raw SimConnect (was #1).** `sim/ai_traffic.py` works on Valen's PC with MSFS 2024 (2026-10).
- **Real airport data for Argentina (was #4).** SABE/SARC/SAAR generated and confirmed fine; taxiways still missing (#14).
- **SimBrief fields (2026-10).** Checked against a real OFP: `navlog` and `alternate` are lists, flight rules are `atc.flight_rules` (`atc.flight_type` = S scheduled), no cruise field (use `general.initial_altitude` + `stepclimb_string`), ICAO SID name is the first `route_ifps` token (`sid_ident` is the 6-char FMS name).
- **Local LLM (Ollama).** Tried and dropped: made the sim stutter, didn't answer. Uninstalled.

- Wind, altimeter and runway-in-use are decided in code and given to the model as facts (`runway.py`, `llm/prompt.py`).
- Scenario replay harness with 6 scenarios (`python -m atc.scenarios`).
- Probe scripts for the PC (`tools/`).
