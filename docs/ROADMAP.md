# Roadmap

Ordered. Each item has a "done when" so it can be checked. Status as of 2026-10-05.
Legend: DONE (verified), WORKING (runs on Valen's PC, loose ends listed), TODO.

## A. Make it real on Valen's PC (Phases 0-1)
1. **Environment.** DONE: Windows venv, `pip install -e .[dev,sim,audio]`, 49 tests pass.
2. **Own telemetry from MSFS 2024.** WORKING: squawk encoding fixed (commit 2300896). Loose ends: the `VERIFY` notes in `sim/simconnect_source.py` (heading radians guard, wind/pressure units) were never formally ticked off; check them with `tools/probe_own.py` if a value looks wrong.
3. **AI traffic.** DONE, verified on Valen's PC: traffic around the airport works fine.
4. **Real airport data.** DONE, verified on Valen's PC: SARC and SABE generate perfectly; SAAR generated too. Hand fields added: `spoken_name` (SABE "Aeroparque", SAAR "Rosario"). `taxi_routes` is empty everywhere (see 9b).

## B. Voice (Phase 3)
5. **Piper.** WORKING: voice chosen, `voices/en_US-libritts-high.onnx` (commit ff34249).
6. **STT.** PARTLY CHECKED (2026-10-05, Piper -> radio filter -> faster-whisper small.en round trip, no mic): ~2.6 s per call on CPU (over budget with LLM + TTS; try base.en), digits fine, names bad ("Martin Air", "Air park", "Atovil for Bravo"). Fixed in code: STT hint with station/telephony/SID/destination, split telephony joined, SID digit soundalikes, any first call on Delivery = clearance request. Still TODO / not reported yet: `FasterWhisperSTT` on CPU, `small.en` vs `base.en`. Done when a spoken call becomes correct text in about 1 s. Watch callsign and number accuracy: the clearance readback check is done by code on the transcript, so STT errors become "negative, I say again".
7. **PTT + radio filter.** TODO / not reported yet: `--ptt` exists; check key handling, squelch click, filter. Done when you can say a call and hear a radio-sounding reply.

## C. Make the ATC good (Phase 4+), the part that decides if it's worth using
8. **Weather.** DONE in code: wind/QNH from the sim, stated only when known. TODO: METAR as alternative source, magnetic vs true (OPEN_QUESTIONS #10).
9. **IFR departure at the origin.** DONE (2026-10-05), all decided by code, model not involved:
   - SimBrief plan via `--simbrief simbrief_last.json` (`flightplan.py`; `tools/probe_simbrief.py` fetches it).
   - Telephony learned from the pilot's first call ("Martinair 4133" -> "Martinair four one three three"), or `--telephony`.
   - Clearance (`clearance.py`): limit, SID + transition, flight planned route, climb via SID / expect filed level 10 min after departure, departure frequency (DEP, else APP), squawk (stable per callsign).
   - Asking the wrong position -> "contact Delivery <freq>". Optional "standby" with a callback 10-25 s later (`--standby`, default 0.3).
   - Readback checked item by item; wrong items -> "negative, I say again, <items>"; after a correction only those items are needed. Correct -> "readback correct, when ready for push and start contact Ground <freq>".
   - Push/start on Ground: approved, or "no clearance received yet, contact Delivery" if not read back.
   - Silence on correct readbacks and acknowledgements ("roger", "wilco", repeating the instruction). Wrong taxi readbacks -> code repeats the instruction ("negative, I say again, ...").
   - ICAO pronunciation done in code (`phrase.py`): digits, niner, "decimal", flight levels, SID names.
   - Audit fixes (2026-10-05): readback "flight level 200, 120.6" was rejected (numbers merged across the comma);
     Tower stayed silent on a first call that repeated Ground's "holding point runway 31" (readbacks now only count on
     the position that gave the instruction); calm wind sent the 31 SID to runway 13 (the plan's runway is kept up to
     5 kt tailwind); calls from another airline flight were answered as ours ("say again your callsign" now);
     registrations are spelled ("Lima Victor Alfa Bravo Charlie"); winds aloft no longer pick the runway; Departure/
     Approach are told when the squawk is wrong.
9b. **Taxi routes.** TODO, next most visible gap: Ground can't name taxiways because no data source has them, and the model must not invent them. Option 1 (quick): fill `taxi_routes` per runway in `airports/SABE.yaml` / `SARC.yaml` from the AD charts (format in the YAML comment). Option 2 (realistic, bigger): read taxiways/parking from MSFS via SimConnect facility data (`SimConnect_RequestFacilityData`, TAXI_PATH/TAXI_NAME/TAXI_PARKING) and compute a route in code.
9c. **Rest of the IFR flight.** TODO: Tower (line up, takeoff clearance, "contact Departure"), Departure/Approach (climb, direct-to, descend, STAR/approach from the plan's `star_ident`), arrival Tower and Ground at the destination. Same rule: code decides the values, model phrases. None of this has been run at SABE yet.
10. **Session state, rest.** First slice DONE (`session.py`: telephony, clearance state, positions contacted). TODO: assigned runway, pattern position, last instruction per position.
11. **Runway-in-use logic.** DONE (`runway.py`). TODO: crosswind limits, preferred runways per airport, SABE noise abatement.
12. **Readback check.** DONE: runway / hold short / holding point / takeoff / landing (`readback.py`) plus the full clearance (`clearance.py`).
13. **Traffic sequencing (VFR focus).** FIRST SLICE DONE (2026-10-05 audit, `sequence.py`, tests/test_sequence.py): code computes who is on the runway and on final for the runway in use (thresholds from OurAirports, now in the YAMLs), tells Tower whether takeoff/landing clearance is ALLOWED, and a guard replaces any "cleared for takeoff/to land" the model still gives ("hold position, traffic on two miles final" / "number two, traffic to follow..."). Rules: arrival inside 3 NM or anyone on the runway blocks takeoff; anyone closer on final or on the runway blocks landing. Fake sim: `/final 3 [rwy]`, `/onrwy`, `/notraffic`. TODO: pattern positions (downwind/base), departures still climbing out, line up and wait, go-arounds, verify with real AI traffic (own aircraft is now filtered by SimConnect object id, VERIFY).
14. **ATIS.** TODO: generate ATIS text, speak it on the ATIS frequency, check the information letter the pilot reports. Currently ATIS frequencies are silent.
15. **Replay test set.** Harness DONE (6 scenarios, `silence_ok` / `expect_silence` supported). TODO: add SABE departure, pattern, go-around and traffic-conflict scenarios.

## D. Latency and cost (Phase 5)
16. **Timing logger.** PARTLY DONE: `[timing] stt + llm + tts` line printed when voice/STT is on. Code-owned replies (clearance, readbacks) take 0 s of LLM time.
17. **Streaming** LLM output into TTS sentence by sentence. TODO.
18. **Model comparison.** First pass DONE (2026-10-05) with `tools/compare_models.py` (Ground taxi turn at SABE, content checks + timing). Results on OpenRouter free:
    - `nvidia/nemotron-3-super-120b-a12b:free`: 2/2, about 0.9 s.
    - `nvidia/nemotron-3-ultra-550b-a55b:free`: 2/2, 1.2 s, but 12 s on a cold start. Valen uses it as main (commit 70be6f5).
    - `nvidia/nemotron-3.5-lightning:free`: invents taxiways ("via Alpha"). Don't use.
    - `google/gemma-4-*:free`: always 429 (rate-limited upstream).
    - `thinkingmachines/inkling-small:free`: 403, only for agentic apps.
    - Gemini and NVIDIA direct: not tested, no keys yet (`GEMINI_API_KEY`, `NVIDIA_API_KEY`).
    - Ollama (local): dropped. It made MSFS stutter and didn't answer; uninstalled.
    - TODO: Claude Haiku 4.5 when paying; check the prompt-caching minimum size.
    - LIMIT FOUND (2026-10-05): OpenRouter free = 50 requests/day TOTAL across all `:free` models (1000/day after a
      one-time 10 credit top-up). About 10-20 LLM calls per flight, so 2-4 flights/day. The client now stops on a
      daily-quota 429 instead of retrying every free model.
    - Latency seen: nemotron ultra 0.9-6 s, once 17.5 s (the 10 s timeout is per socket read, not total).

## E. Coverage (Phase 6) and extras
19. SABE/SARC manual pass from the AIP charts: taxi routes, real departure frequency (SABE currently uses APP 120.6 as the departure frequency, VERIFY), procedures, reporting points.
20. Spanish phraseology prompt and Piper Spanish voice. Then Norwegian for practice.
21. Optional Phase 7: VATSIM "who covers me" helper (see docs/PLAN.md).

## Risks to keep in mind
- Free model tiers rate-limit at busy hours (Gemma already does). Keep a fallback model in `ATC_LLM_MODEL`.
- LLM output varies per run, so tests check content rules, not exact strings. Code-owned replies are exact and tested exactly.
- STT mistakes on callsigns/numbers turn into false "negative" corrections. Watch for this when voice input is tested.
- Piper quality is good for a prototype but not identical to cloud neural voices.
