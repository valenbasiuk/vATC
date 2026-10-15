"""FS Traffic's injection on/off from the launcher (atc.own.fstraffic): only the control centre moves."""

from atc import config
from atc.own import fstraffic


def _community(tmp_path):
    c = tmp_path / "Packages" / "Community"
    for name in ("justflight-fstraffic-module", "justflight-fstraffic-controlcentre", "fsltl-traffic-base",
                 "some-scenery-sabe"):
        (c / name).mkdir(parents=True)
        (c / name / "manifest.json").write_text("{}")
    return c


def test_off_parks_only_the_control_centre_and_on_brings_it_back(tmp_path):
    c = _community(tmp_path)
    assert fstraffic.status(c) == "on"
    msg = fstraffic.set_injection(False, c)
    assert "off" in msg and "justflight-fstraffic-controlcentre" in msg
    assert not (c / "justflight-fstraffic-controlcentre").exists()
    assert (tmp_path / "Packages" / "vATC-parked" / "justflight-fstraffic-controlcentre" / "manifest.json").exists()
    for kept in ("justflight-fstraffic-module", "fsltl-traffic-base", "some-scenery-sabe"):
        assert (c / kept).is_dir()  # models, schedules and everything else stay
    assert fstraffic.status(c) == "off"
    assert "already off" in fstraffic.set_injection(False, c)
    fstraffic.set_injection(True, c)
    assert (c / "justflight-fstraffic-controlcentre" / "manifest.json").exists()
    assert fstraffic.status(c) == "on"


def test_without_fs_traffic(tmp_path):
    c = tmp_path / "Community"
    c.mkdir()
    assert fstraffic.status(c) == "not installed"
    assert "already" in fstraffic.set_injection(False, c)


def test_launcher_settings_for_our_traffic():
    cfg = config.Config(own_traffic=True, own_apron=3, own_lights="events")
    env = cfg.env()
    assert env["ATC_OWN_APRON"] == "3" and env["ATC_OWN_LIGHTS"] == "events"
    assert config.fstraffic_wanted(cfg) is False  # auto: FS Traffic off while ours fly
    assert config.fstraffic_wanted(config.Config(own_traffic=False)) is True
    assert config.fstraffic_wanted(config.Config(fstraffic="leave")) is None
    assert config.fstraffic_wanted(config.Config(own_traffic=False, fstraffic="off")) is False
    assert "ATC_OWN_APRON" not in config.Config(own_traffic=False).env()
