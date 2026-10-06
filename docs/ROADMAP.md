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
9b. **Taxi routes.** WORKING (2026-10-05, `taxi.py`, code-owned): graph from OpenStreetMap (`tools/fetch_osm_taxi.py ICAO` -> `airports/osm/ICAO.json`; done for SABE, SAAR, SARC), Dijkstra with a penalty per taxiway change, goal = runway holding point at the departure end (else the taxiway node next to the threshold). Ground: "taxi to holding point runway 31 via Kilo, Alfa, QNH ..."; after landing "taxi to stand 12 via ..." (requested stand, else a free one). Taxiway names are read-back checked. Hand `taxi_routes` in the YAML always win. Coverage: SABE good, checked against Valen's LIDO chart 2026-10-05 (A parallel ~107 m NE of the centerline; B C D E F H I J K L M as on the chart; OSM's stand lead-in "1" is filtered, designators must start with a letter; apron -> 31 "via Kilo, Alfa", -> 13 "via Alfa"); SAAR partial (runway 20 end not connected: clearance without names); SARC no names in OSM. TODO: MSFS's own data via `tools/probe_taxi.py` (UNTESTED; needs the MSFS 2024 SDK SimConnect.dll via `--dll`, the bundled one has no facility API) -> `airports/msfs/ICAO.json`, preferred over OSM; hold short of crossed runways.
9c. **Rest of the IFR flight.** WORKING in the fake sim (2026-10-05, `flow.py`, `world.py`, all code-owned):
   - One run covers the flight: origin, destination and alternate are loaded (missing YAMLs generated from `data/`), the tuned frequency picks the airport; area control from `airspace/*.yaml` (`airspace/SAEF.yaml` has NO frequencies yet: fill from AIP ENR 2.1, until then Departure -> Control is skipped).
   - Telemetry watcher (1 s, in main `_Callbacks`) detects takeoff/landing and hands off: Tower -> Departure (700 ft AGL), Departure -> Control (FL100 / 30 NM), -> destination Approach (40 NM) or Tower (18 NM if no Approach), Approach -> Tower (12 NM), Tower -> Ground (vacated, < 40 kt). Said again once after 20 s if the frequency isn't changed; never while PTT is held.
   - Check-ins: "radar contact, climb via SID" / arrival "radar contact, expect runway 20, QNH ..."; wrong squawk -> "squawk 2235", then "radar contact" when the code shows.
   - Tower: takeoff ("wind ..., runway 31, cleared for takeoff" or "hold position, traffic on two miles final") and landing ("cleared to land" on final, "number two, traffic to follow..." or "continue approach, report final").
   - Fake sim: `/near SAAR 30 6000` puts you on an arrival.
   - After Valen's first real-sim flight (2026-10-05, `enroute.py`, tests/test_arrival.py), also code-owned:
     departure check-in "climb via SID to flight level 200" (filed level, no Control on file); "request direct X"
     only to route fixes still ahead (SimBrief navlog lat/lon), else "behind you" / "not on your route";
     "request higher"; descent at the navlog's TOD ("descend to three thousand feet, QNH ..., expect vectors runway 02",
     transition altitude from the OFP), also with the check-in if already past TOD; "request descent" before TOD ->
     "expect descent in N miles"; vectors to a point 10 NM out / 3 NM to the side, then "turn right heading 340,
     maintain 3000 until established, cleared approach runway 02"; Approach -> Tower once established; Tower clears
     to land on its own at 6 NM; after touchdown below 60 kt "welcome to Rosario, vacate via X when able" (first exit
     ahead on the taxi map, else backtrack). Destination without Approach (SAAR TWR/APP): Tower takes over at 40 NM
     and does the approach work. "On final ..." is a report that gets an answer, not a readback.
   - TODO: STAR/approach procedures and off-route fixes need a nav database (MSFS facility API, or Little Navmap's
     SQLite db); approach type is said generically ("cleared approach"); SAAR mag variation unknown (headings true);
     line up and wait, go-around, holding.
10. **Session state, rest.** First slice DONE (`session.py`: telephony, clearance state, positions contacted). TODO: assigned runway, pattern position, last instruction per position.
11. **Runway-in-use logic.** DONE (`runway.py`). TODO: crosswind limits, preferred runways per airport, SABE noise abatement.
12. **Readback check.** DONE: runway / hold short / takeoff / landing / taxi route (`readback.py`) plus the full clearance (`clearance.py`).
    Fixed from Valen's first session (2026-10-05, replayed in tests/test_valen_session.py): an instruction read back
    correctly is not checked again (`session.acked_atc`); corrections don't nest; "three one via alfa" and "holding point
    for 31" are valid ICAO readbacks ("holding point" is a clearance limit, not an item; FAA "hold short" still is);
    echoed "when ready" isn't a request; "LATAM1302" is split for the callsign check; "finished pushback, ready to taxi"
    is a taxi request; "on holding point ..." to Ground -> "contact Tower" (also by telemetry when stopped there);
    a model reply that is only "<callsign>, roger" is dropped; learned telephony is kept in data/telephony_learned.json.
    Readback demanded (ICAO Doc 4444 4.5.7.5): "roger" to the IFR clearance (or push/taxi before reading it back) ->
    "read back the clearance"; "roger"/"wilco" to taxi, runway, takeoff/landing, hold short, QNH, squawk, climb/descend/
    heading -> "read back", and the next call is checked against that instruction (`session.readback_due`). QNH and
    squawk are now readback items. Handoffs, approvals and "hold position" need none.
13. **Traffic sequencing (VFR focus).** FIRST SLICE DONE (2026-10-05 audit, `sequence.py`, tests/test_sequence.py): code computes who is on the runway and on final for the runway in use (thresholds from OurAirports, now in the YAMLs), tells Tower whether takeoff/landing clearance is ALLOWED, and a guard replaces any "cleared for takeoff/to land" the model still gives ("hold position, traffic on two miles final" / "number two, traffic to follow..."). Rules: arrival inside 3 NM or anyone on the runway blocks takeoff; anyone closer on final or on the runway blocks landing. Fake sim: `/final 3 [rwy]`, `/onrwy`, `/notraffic`. TODO: pattern positions (downwind/base), departures still climbing out, line up and wait, go-arounds, verify with real AI traffic (own aircraft is now filtered by SimConnect object id, VERIFY).
14. **ATIS.** TODO: generate ATIS text, speak it on the ATIS frequency, check the information letter the pilot reports. Currently ATIS frequencies are silent.
15. **Replay test set.** Harness DONE (6 scenarios, `silence_ok` / `expect_silence` supported). TODO: add SABE departure, pattern, go-around and traffic-conflict scenarios.

## D. Latency and cost (Phase 5)
16. **Timing logger.** PARTLY DONE: `[timing] stt + llm + tts` line printed when voice/STT is on. Code-owned replies (clearance, readbacks) take 0 s of LLM time.
17. **Streaming.** TTS DONE (2026-10-05): Piper plays sentence by sentence (the clearance starts after 1.2 s instead of 4.4 s; playback through `sd.OutputStream` not heard yet). LLM total deadline `ATC_LLM_DEADLINE_S` (default 12 s) across all fallbacks. TODO: LLM token streaming (most replies are code-owned now, so low value).
17b. **Fact check** DONE (`factcheck.py`): every LLM reply's numbers, aircraft types and taxiway names must appear in what the model was given; else one retry naming the problem, then "<callsign>, say again".
17c. **Traffic information by code** DONE (`traffic.py`): "traffic, two o'clock, three miles, opposite direction, Airbus three twenty, one thousand feet above" (within 8 NM / 3000 ft). AI type/airline/flight number read from SimConnect (`ATC MODEL`, `ATC AIRLINE`, `ATC FLIGHT NUMBER`; VERIFY with probe_traffic, falls back to the basic layout if rejected).
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
    - Multi-provider (2026-10-05): `ATC_LLM_MODEL="groq:...,gemini:...,openrouter:..."`, each with its own key
      (`GROQ_API_KEY`, `GEMINI_API_KEY`, `CEREBRAS_API_KEY`, `NVIDIA_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`,
      `ANTHROPIC_API_KEY`; read from the Windows user environment too). Free quotas stack. With clearance, taxi,
      takeoff/landing, check-ins, handoffs and traffic in code, a flight needs only a few LLM calls (VFR pattern work,
      questions). `tools/compare_models.py` scores those LLM-owned turns (VFR inbound SARC, wind/QNH question SABE).
    - Free-key results 2026-10-05 (2 runs x 2 turns, with fact check + landing guard on):
      4/4 groq:qwen/qwen3.8-27b 0.7 s (but says "cleared to land" 10 NM out: the guard fixes it to "report final"),
      4/4 nvidia:nvidia/nemotron-3-super-120b-a12b 1.0 s, 4/4 gemini:gemini-flash-lite-latest 1.1 s,
      4/4 openrouter nemotron-3-super:free 1.1 s, 4/4 gemini:gemini-3.5-flash 4.9 s.
      Without the guard groq gpt-oss-20b/120b scored 0/4 (early landing clearance, "wind three zero" for 030).
      Gone/404 now: gemini-2.5-*, nvidia meta/llama-3.3-70b. Groq needs a User-Agent (Cloudflare 403 1010).
      Gemini thinking off = reasoning_effort "minimal" ("none" is a 400 on 3.5 Lite). Recommended order:
      gemini flash-lite-latest, nvidia nemotron super, openrouter nemotron super :free, groq qwen3.8.
    - Winds are said magnetic where `mag_var_deg` is in the YAML (SABE -10 from the LIDO chart); SARC/SAAR TODO.

## E. Coverage (Phase 6) and extras
19. SABE/SARC manual pass from the AIP charts: taxi routes, real departure frequency (SABE currently uses APP 120.6 as the departure frequency, VERIFY), procedures, reporting points.
20. Spanish phraseology prompt and Piper Spanish voice. Then Norwegian for practice.
21. Optional Phase 7: VATSIM "who covers me" helper (see docs/PLAN.md).

## Risks to keep in mind
- Free model tiers rate-limit at busy hours (Gemma already does). Keep a fallback model in `ATC_LLM_MODEL`.
- LLM output varies per run, so tests check content rules, not exact strings. Code-owned replies are exact and tested exactly.
- STT mistakes on callsigns/numbers turn into false "negative" corrections. Watch for this when voice input is tested.
- Piper quality is good for a prototype but not identical to cloud neural voices.
