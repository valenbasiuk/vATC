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
