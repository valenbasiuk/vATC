"""The launcher (improvement plan block 2): settings -> the same command line as by hand, the live status the ATC
process writes, how the log lines are coloured. The window itself is Tk: not opened here."""

from atc import config
from atc.main import status_snapshot
from atc.models import Frequency
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.world import World
from tests.test_flight import ORIGIN


def test_settings_become_the_usual_command_line_and_environment(tmp_path):
    cfg = config.Config(sim=True, airport="", voice="voices/x.onnx", ptt=True, ptt_key="f10", ptt_joy=19,
                        ptt_joy_device=2, mic="1", audio_out="4", own_traffic=True, own_factor=1.5, own_max=6,
                        metar="off", own_tug=False, own_models="fsltl", atis_question=False)
    argv = cfg.to_argv("S.json")
    assert argv[:1] == ["--sim"] and "--airport" not in argv
    assert argv[argv.index("--ptt-key") + 1] == "f10" and argv[argv.index("--ptt-joy") + 1] == "19"
    assert argv[argv.index("--ptt-joy-device") + 1] == "2" and argv[argv.index("--mic") + 1] == "1"
    assert argv[argv.index("--own-factor") + 1] == "1.5" and argv[argv.index("--own-max") + 1] == "6"
    assert argv[argv.index("--status-file") + 1] == "S.json" and "--stdin" in argv
    env = cfg.env()
    assert env["ATC_METAR"] == "off" and env["ATC_OWN_TUG"] == "off" and env["ATC_OWN_MODELS"] == "fsltl"
    assert env["ATC_ATIS_QUESTION"] == "0"
    p = config.save(cfg, tmp_path / "c.json")
    back = config.load(p)
    assert back == cfg
    assert config.load(tmp_path / "missing.json") == config.Config()


def test_fake_sim_needs_an_airport_on_the_command_line():
    argv = config.Config(sim=False, airport="sabe", ptt=False, voice="").to_argv()
    assert argv[:2] == ["--airport", "SABE"] and "--ptt" not in argv and "--voice" not in argv


def test_status_snapshot_for_the_live_panel():
    from dataclasses import replace

    apt = replace(ORIGIN, frequencies=ORIGIN.frequencies + [Frequency("ATIS", 127.6)])
    sim = FakeSim(apt, callsign="MAR4133")
    sim.update(lat=-34.5638, lon=-58.4072, com1_mhz=121.9, wind_dir_deg=124.0, wind_kt=8.0, qnh_hpa=1015.0)
    snap = status_snapshot(World([apt]), sim, Session(callsign="MAR4133", telephony="Martinair"))
    assert snap["station"] == {"icao": "SATS", "role": "ground", "name": "Testa Ground"}
    assert snap["airport"]["icao"] == "SATS" and {"kind": "TWR", "mhz": 118.85} in snap["airport"]["frequencies"]
    assert snap["runway"] == "13" and snap["atis"]["text"].startswith("Testa information")


def test_log_lines_are_told_apart():
    from atc.gui import _line_tag

    assert _line_tag("YOU> request taxi") == "you"
    assert _line_tag("ATC> Argentina one two, taxi") == "atc"
    assert _line_tag("Argentina one two one six> Push and start approved") == "ai"
    assert _line_tag("[own traffic: ARG1216 gone]") == "sys"
