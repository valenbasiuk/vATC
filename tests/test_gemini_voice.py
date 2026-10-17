"""Gemini voices for the controllers (ATC_VOICE_ENGINE=gemini): each position its own, the local one as fallback."""

import numpy as np

from atc import voices
from atc.audio import engines, tts


def _bank(tmp_path):
    bank = voices.VoiceBank(tmp_path)
    bank.pools["generic"] = [voices.Voice("x.onnx", None, accent="generic"),
                             voices.Voice("x.onnx", 1, accent="generic", female=True)]
    return bank


def test_controllers_get_their_own_gemini_voice_with_a_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ATC_VOICE_ENGINE", "gemini")
    bank = _bank(tmp_path)
    gnd, twr = bank.controller("SABE", "ground", "AR"), bank.controller("SABE", "tower", "AR")
    assert gnd.engine == twr.engine == "gemini" and gnd.name != twr.name
    assert gnd.fallback is not None and gnd.fallback.engine == "piper"
    assert bank.controller("SABE", "ground", "AR").name == gnd.name  # the same person all session
    assert bank.controller("SABE", "atis", "AR").engine != "gemini"  # the ATIS loops: local voice
    assert bank.pilot("ARG1234", "AR").engine != "gemini"
    monkeypatch.setenv("ATC_VOICE_ENGINE", "auto")
    assert bank.controller("SABE", "ground", "AR").engine != "gemini"


def test_one_request_per_transmission_and_the_fallback_speaks_on_failure(tmp_path, monkeypatch):
    calls = []

    def fake(engine, name, text, rate=1.0):
        calls.append(text)
        if len(calls) > 1:
            raise RuntimeError("Gemini TTS: gemini-3.8-flash-tts 429")
        return np.zeros(2400, np.float32), 24000

    monkeypatch.setattr(engines, "synthesize", fake)
    s = object.__new__(tts.PiperTTS)
    s.length_scale, s._broken, s._down, s._kokoro, s._kokoro_failed = 1.0, set(), {}, None, False
    s._voice = None
    local = []
    real_chunks = tts.PiperTTS._chunks

    def chunks(self, text, voice=None):
        if voice is not None and getattr(voice, "engine", "piper") == "piper":
            local.append(text)
            yield np.zeros(10, np.float32), 22050
            return
        yield from real_chunks(self, text, voice)

    monkeypatch.setattr(tts.PiperTTS, "_chunks", chunks)
    fb = voices.Voice("x.onnx", None)
    v = voices.Voice("x.onnx", None, engine="gemini", name="Orus", fallback=fb)
    list(s._chunks("Aeroperu one three zero two, taxi. QNH one zero two two.", v))
    assert calls == ["Aeroperu one three zero two, taxi. QNH one zero two two."] and not local
    list(s._chunks("Second call. Two sentences.", v))  # fails: the local voice says it all
    assert local == ["Second call. Two sentences."]
    list(s._chunks("Third call.", v))  # resting: straight to the local voice, no request
    assert len(calls) == 2 and local[-1] == "Third call."
