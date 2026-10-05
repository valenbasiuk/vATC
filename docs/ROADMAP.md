# Roadmap after the skeleton

Ordered. Each item has a "done when" so it can be checked. Time estimates are guesses.

Do docs/PRE_IDE_CHECKLIST.md first. It covers items 1-7 below as checks you run yourself and bring results from.

## A. Make it real on Valen's PC (Phases 0-1)
1. **Environment.** 64-bit Python 3.11+, `pip install -e .[dev,sim]`. Done when `pytest` passes on Windows.
2. **Own telemetry from MSFS 2024.** Run `SimConnectSource.own()` in a loop and print it. Fix the VERIFY items (indexed simvar names, heading units, COM frequency, squawk encoding). Done when values match the cockpit.
3. **AI traffic.** DONE, verified on Valen's PC (2026-10): traffic around the airport works fine.
4. **Real airport data.** DONE, verified on Valen's PC (2026-10): SARC and SABE generate perfectly.

## B. Voice (Phase 3)
5. **Piper.** `PiperTTS` now uses the Python API and keeps the voice loaded (written from the docs, never run). Pick a US voice with `tools/probe_voice.py`, fix whatever breaks.
6. **STT.** `FasterWhisperSTT` on CPU, measure speed for `small.en` vs `base.en`. Done when a spoken call becomes correct text in under about 1 s.
7. **PTT + radio filter.** Wire `PushToTalk` into the loop; add squelch click; compare FFT mask against a proper filter. Done when you can say a call and hear a radio-sounding reply.

## C. Make the ATC good (Phase 4+), the part that decides if it's worth using
8. **Weather.** DONE in the skeleton: wind and QNH are read from the sim, put in CONTEXT, and stated only when known. TODO: check the values against the sim (checklist C1), consider METAR as an alternative source, magnetic vs true (OPEN_QUESTIONS #10).
9a. **IFR departure flow.** DONE (2026-10): SimBrief plan (`--simbrief`), `session.py` (telephony learned from the pilot's call, clearance state), `clearance.py` (clearance issued by code from the plan, item-by-item readback check, "negative, I say again" corrections, wrong position -> "contact Delivery", push/start gated on a read-back clearance), `phrase.py` (ICAO number pronunciation). Silence on correct readbacks and acknowledgements. TODO: taxi routes in the airport YAML (`taxi_routes`, SABE/SARC from the AD charts), or read taxiways from MSFS (SimConnect facility data).
9. **Session state.** Keep a small `Session` (assigned runway, squawk, clearance state, position in pattern, who was last told what). Update it from structured LLM output (JSON alongside the spoken text) so the model does not have to re-derive everything from chat history.
10. **Runway-in-use logic.** DONE (`runway.py`): best headwind, longest runway in calm wind, passed as fact. TODO: crosswind limits, prefer-calm-wind-runway lists per airport, noise-abatement rules for SABE.
11. **Readback check.** Compare the pilot's readback to the clearance in code, then tell the LLM "readback correct / missing runway".
12. **Traffic sequencing.** Compute in code, from `traffic()`, who is on final, on the runway, in the pattern, and give the LLM facts like "N111 on 2 NM final runway 09". The LLM phrases it; code decides it. This is the VFR weak spot, so test it hardest.
13. **ATIS.** Generate ATIS text (or let the LLM) once, speak it on the ATIS frequency. Currently ATIS frequencies are silent.
14. **Replay test set.** Harness DONE (`python -m atc.scenarios`, 6 starter scenarios in `scenarios/`). TODO: grow it every time a real flight produces a wrong reply; add pattern, go-around, readback and traffic-conflict scenarios.

## D. Latency and cost (Phase 5)
15. **Timing logger** per stage (STT, LLM, TTS) in every turn. Target total under 3-4 s.
16. **Streaming** LLM output into TTS sentence by sentence.
17. **Model comparison** with the replay set: free OpenRouter model vs Gemini API free tier vs Claude Haiku 4.5. Check prompt-caching minimum size before counting on it.

## E. Coverage (Phase 6) and extras
18. SABE/SARC manual pass from the AIP charts (frequencies, procedures, reporting points).
19. Spanish phraseology prompt and Piper Spanish (Argentine if available) voice. Then Norwegian for practice.
20. Optional Phase 7: VATSIM "who covers me" helper (see docs/PLAN.md).

## Risks to keep in mind
- Python SimConnect packages may not cover AI traffic. Budget time for ctypes or a small helper in another language.
- LLM output varies per run, so tests must check content rules, not exact strings.
- Piper quality is good for a prototype but not identical to cloud neural voices.
