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
