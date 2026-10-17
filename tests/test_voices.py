"""Voice bank: accent pools from the models' speaker lists, and voices/blacklist.txt (no audio needed)."""

import json

from atc.voices import VoiceBank, accents_for


def _fake_l2arctic(folder):
    (folder / "en_US-l2arctic-medium.onnx").write_bytes(b"")
    (folder / "en_US-l2arctic-medium.onnx.json").write_text(json.dumps({
        "language": {"family": "en", "code": "en_US"}, "num_speakers": 4,
        "speaker_id_map": {"EBVS": 0, "ERMS": 1, "MBMPS": 2, "BWC": 3}}), encoding="utf-8")


def test_pools_by_first_language(tmp_path):
    _fake_l2arctic(tmp_path)
    bank = VoiceBank(tmp_path)
    assert {k: len(v) for k, v in bank.pools.items()} == {"Spanish": 3, "Mandarin": 1}
    assert bank.controller("SABE", "tower", "AR").accent == "Spanish"
    assert accents_for("AR") == ["Spanish"]


def test_blacklist_drops_voices_and_whole_accents(tmp_path):
    _fake_l2arctic(tmp_path)
    (tmp_path / "blacklist.txt").write_text("# too hard to understand\n"
                                            "en_US-l2arctic-medium#0  # mumbles\n"
                                            "accent:Mandarin\n", encoding="utf-8")
    bank = VoiceBank(tmp_path)
    assert [v.key for v in bank.pools["Spanish"]] == ["en_US-l2arctic-medium#1", "en_US-l2arctic-medium#2"]
    assert "Mandarin" not in bank.pools
    (tmp_path / "blacklist.txt").write_text("accent:Spanish\n", encoding="utf-8")
    assert VoiceBank(tmp_path).controller("SABE", "tower", "AR") is None  # no generic pool here: the default voice


def _fake_spanish_pool(folder):
    (folder / "en_US-l2arctic-medium.onnx").write_bytes(b"")
    (folder / "en_US-l2arctic-medium.onnx.json").write_text(json.dumps({
        "language": {"family": "en", "code": "en_US"}, "num_speakers": 4,
        "speaker_id_map": {"EBVS": 0, "ERMS": 1, "MBMPS": 2, "NJS": 3}}), encoding="utf-8")
    (folder / "en_US-libritts-high.onnx").write_bytes(b"")
    (folder / "en_US-libritts-high.onnx.json").write_text(json.dumps({
        "language": {"family": "en", "code": "en_US"}, "num_speakers": 50}), encoding="utf-8")


def test_nobody_on_the_radio_shares_a_voice(tmp_path):
    """Valen 2026-10-08: at SABE several speakers had the same voice (Spanish pool: 2 men, 2 women)."""
    _fake_spanish_pool(tmp_path)
    bank = VoiceBank(tmp_path)
    clock = [0.0]
    bank.clock = lambda: clock[0]
    speakers = [bank.controller("SABE", r, "AR") for r in ("ground", "tower", "delivery", "approach")]
    speakers += [bank.pilot(cs, "AR") for cs in ("ARG1216", "ARG1780", "FBZ5231", "JES3046", "AEP1890", "LVKXT")]
    heard = [(v.key, v.pitch) for v in speakers]
    assert len(set(heard)) == len(heard)  # ten different voices
    assert all(v.accent == "Spanish" for v in speakers)  # 4 people x 3 pitches = 12 before the generic pool
    assert bank.pilot("ARG1216", "AR") == speakers[4]  # the same pilot keeps the same voice
    more = [bank.pilot(f"ARG{n}", "AR") for n in range(100, 104)]
    assert {v.accent for v in more} == {"Spanish", "generic"}  # 12 Spanish voices taken: then generic ones
    clock[0] += 3600.0  # an hour later the pilots of before are gone: their voices are free again
    assert bank.pilot("ARG2000", "AR").accent == "Spanish"


def test_pitch_shift_keeps_the_length_it_was_synthesized_for():
    import pytest

    np = pytest.importorskip("numpy")
    from atc.audio.tts import shift_pitch

    x = np.sin(np.arange(22050 * 108 // 100) * 2 * np.pi * 200 / 22050).astype(np.float32)  # 1.08 s at 200 Hz
    y = shift_pitch(x, 1.08)
    assert len(y) == 22050  # synthesized 1.08x slower, back to 1 s
    crossings = np.sum((y[:-1] < 0) & (y[1:] >= 0))
    assert 214 <= crossings <= 218  # 216 Hz


def _fake_kokoro(folder):
    (folder / "kokoro-v1.0.onnx").write_bytes(b"")
    (folder / "voices-v1.0.bin").write_bytes(b"")


def test_kokoro_first_for_american_and_british_l2arctic_first_for_spanish(tmp_path, monkeypatch):
    monkeypatch.delenv("ATC_VOICE_ENGINE", raising=False)
    _fake_spanish_pool(tmp_path)
    _fake_kokoro(tmp_path)
    bank = VoiceBank(tmp_path)
    assert bank.controller("KSFO", "tower", "US").kokoro
    assert bank.pilot("BAW286", "GB").kokoro
    assert not bank.controller("SABE", "tower", "AR").kokoro  # real accented English first
    assert bank.pilot("GLO1234", "BR").accent == "Portuguese"  # only Kokoro has Brazilians
    assert any(v.kokoro for v in bank.pools["Spanish"])  # ... and more Spanish people after L2-ARCTIC's
    blends = [v for v in bank.pools["generic"] if "+" in v.style]
    assert len(blends) >= 10 and bank.pilot("N123AB", None).kokoro
    assert not any(v.style == "am_eric" for p in bank.pools.values() for v in p)  # D grade: blended only
    assert all(v.female == (v.style[1] == "f") for p in bank.pools.values() for v in p if v.kokoro)


def test_kokoro_off_by_setting_or_without_its_voices_file(tmp_path, monkeypatch):
    _fake_spanish_pool(tmp_path)
    _fake_kokoro(tmp_path)
    monkeypatch.setenv("ATC_VOICE_ENGINE", "piper")
    assert not any(v.kokoro for p in VoiceBank(tmp_path).pools.values() for v in p)
    monkeypatch.delenv("ATC_VOICE_ENGINE")
    (tmp_path / "voices-v1.0.bin").unlink()
    assert not any(v.kokoro for p in VoiceBank(tmp_path).pools.values() for v in p)


def test_kokoro_parts_start_with_the_callsign_and_keep_every_word():
    from atc.audio.tts import kokoro_parts

    text = "Aerolineas one two three four, wind one four zero degrees one zero knots, runway one three, " \
           "cleared for takeoff. Contact departure one two zero decimal six."
    parts = kokoro_parts(text)
    assert parts[0][0] == "Aerolineas one two three four,"
    assert " ".join(p for p, _ in parts).split() == text.split()
    assert parts[-1][1] > parts[0][1]  # a longer pause after a sentence than after a clause


# --- J1: voices of other engines (Kokoro / Edge) in the same pools -------------------------------------------------

def test_extra_voices_join_the_pools_when_their_engine_is_there(tmp_path, monkeypatch):
    from atc.audio import engines

    _fake_spanish_pool(tmp_path)
    (tmp_path / "extra_voices.txt").write_text("# winners of tools/voice_compare.py\n"
                                               "Spanish     edge:es-AR-TomasNeural   # Argentine controllers\n"
                                               "American F  kokoro:af_heart\n", encoding="utf-8")
    monkeypatch.setenv("ATC_VOICE_ENGINE", "piper")  # no Kokoro
    monkeypatch.setattr(engines, "available", lambda e: None if e == "edge" else "pip install edge-tts")
    bank = VoiceBank(tmp_path)
    keys = [v.key for v in bank.pools["Spanish"]]
    assert "edge:es-AR-TomasNeural" in keys and "American" not in bank.pools
    assert bank.skipped[0].startswith("kokoro:af_heart (Kokoro not downloaded")
    v = next(v for v in bank.pools["Spanish"] if v.engine == "edge")
    assert v.label.startswith("edge:es-AR-TomasNeural Spanish")


def test_an_engine_voice_is_spoken_by_its_engine_and_falls_back_to_piper(monkeypatch):
    import numpy as np

    from atc.audio import engines
    from atc.audio.tts import PiperTTS
    from atc.voices import Voice

    calls = []

    def fake(engine, name, text, rate=1.0):
        calls.append((engine, name, text))
        if len(calls) > 1:
            raise RuntimeError("offline")
        return np.zeros(2400, dtype=np.float32), 24000

    monkeypatch.setattr(engines, "synthesize", fake)
    tts = PiperTTS.__new__(PiperTTS)  # no Piper model needed for this
    tts.length_scale, tts._broken = 1.0, set()

    class _Chunk:
        audio_int16_bytes = np.zeros(220, dtype=np.int16).tobytes()
        sample_rate = 22050

    class _Piper:
        def synthesize(self, text, **kw):
            calls.append(("piper", None, text))
            yield _Chunk()

    tts._voice = _Piper()
    voice = Voice("", None, accent="American", engine="edge", name="en-US-GuyNeural")
    out = list(tts._chunks("Speedbird two eight six, contact tower. Good day.", voice))
    assert [c[:2] for c in calls] == [("edge", "en-US-GuyNeural"), ("edge", "en-US-GuyNeural"), ("piper", None)]
    assert calls[-1][2] == "Good day." and len(out) == 2 and "edge" in tts._broken
    calls.clear()
    list(tts._chunks("Roger.", voice))  # broken engine: straight to Piper
    assert calls == [("piper", None, "Roger.")]


def test_engine_specs():
    from atc.audio.engines import parse_spec

    assert parse_spec("edge:en-GB-RyanNeural") == ("edge", "en-GB-RyanNeural")
    assert parse_spec("kokoro:bm_george") == ("kokoro", "bm_george")
    assert parse_spec("C:/voices/en_US-ryan-high.onnx") == ("piper", "C:/voices/en_US-ryan-high.onnx")
