"""The whole IFR flight SABE -> SAAR in the fake sim, through main.handle and the watcher, as Valen flies it:
clearance, push, taxi, Tower, Departure, Ezeiza Control, Rosario (TWR/APP), vectors, approach, landing, vacate.
Guards the interplay of everything the watcher does each tick (ground conflicts, holds, flight following, FIRs,
chatter...): no LLM call, nothing said that doesn't belong, the key transmissions in order."""

from pathlib import Path

from atc.audio.tts import PrintTTS
from atc.geo import bearing_deg
from atc.main import _Callbacks, handle
from atc.sequence import threshold
from atc.session import Session
from atc.sim.fake import FakeSim
from atc.taxi import _holding_points
from atc.world import load_world

from test_arrival import PLAN

ROOT = Path(__file__).resolve().parents[1]


class _Q(PrintTTS):
    def say(self, text):
        pass

    def say_as(self, text, who="ATC", voice=None):
        pass


class _NoLLM:
    calls = 0

    def complete(self, messages):
        _NoLLM.calls += 1
        return "LLM"


def test_sabe_to_rosario_end_to_end():
    _NoLLM.calls = 0
    world = load_world("SABE", ROOT / "airports", PLAN, use_navdb=False)
    sabe, saar = world.get("SABE"), world.get("SAAR")
    sim = FakeSim(sabe, callsign="MAR4133")
    s = Session(callsign="MAR4133", plan=PLAN, telephony="Martinair", dest_name="Rosario")
    cb = _Callbacks(world, sim, _Q(), [], s)
    said: list[str] = []
    now = [0.0]

    def say(text):
        apt = world.pick(sim.own())[0]
        r = handle(apt, sim, _NoLLM(), _Q(), cb.history, text, session=s, world=world)
        cb.after_turn(r is not None)
        if r:
            said.append(r)
        return r

    def tick(n=1, dt=2.0):
        for _ in range(n):
            now[0] += dt
            r = cb.tick(now[0])
            if r:
                said.append(r)

    def readback(text):
        assert say(text.split(", ", 1)[1] + ", Martinair 4133") is None  # a correct readback: silence

    def fly(lat, lon, alt, toward=None, hdg=None, gs=250):
        hdg = hdg if hdg is not None else bearing_deg(lat, lon, *toward)
        sim.update(lat=lat, lon=lon, alt_msl_ft=alt, alt_agl_ft=alt - 20, heading_deg=hdg, gs_kt=gs, on_ground=False)

    sim.update(lat=sabe.lat + 0.004, lon=sabe.lon + 0.004, wind_dir_deg=300, wind_kt=10, qnh_hpa=1015,
               com1_mhz=129.3, squawk="2000", zulu_s=50000)
    tick()
    clearance = say("Aeroparque Delivery, Martinair 4133, request IFR clearance to Rosario")
    say(clearance.split(", ", 1)[1] + ", Martinair 4133")  # -> "readback correct, ... contact Ground"
    sim.update(com1_mhz=121.9, squawk=PLAN.squawk)
    tick()
    say("Aeroparque Ground, Martinair 4133, request push and start")
    assert say("push and start approved, Martinair 4133") is None
    readback(say("Martinair 4133, request taxi"))
    rwy = next(r for r in sabe.runways if r.ident == "31")
    net = world.taxi.get("SABE")
    hp = sorted(_holding_points(net, sabe, rwy))[0]
    sim.update(lat=net.nodes[hp][0], lon=net.nodes[hp][1], gs_kt=0)
    tick(3)
    sim.update(com1_mhz=118.85)
    tick()
    readback(say("Aeroparque Tower, Martinair 4133, holding point 31, ready for departure"))
    tl = threshold(sabe, rwy)
    sim.update(lat=tl[0], lon=tl[1], heading_deg=rwy.heading_deg, gs_kt=60)
    tick()
    fly(-34.53, -58.45, 1500, hdg=310, gs=180)
    tick(3)
    sim.update(com1_mhz=120.6)
    tick()
    say("Aeroparque Approach, Martinair 4133, passing 2000 climbing")
    fly(-34.30, -58.80, 11000, hdg=300, gs=300)
    tick(3)
    sim.update(com1_mhz=135.5)
    tick()
    say("Ezeiza Control, Martinair 4133, passing flight level 110")
    fly(-33.70, -59.67, 20000, toward=(saar.lat, saar.lon), gs=420)
    tick(3)
    fly(-33.30, -60.25, 20000, toward=(saar.lat, saar.lon), gs=420)
    tick(3)
    sim.update(com1_mhz=118.7)
    tick()
    readback(say("Rosario Tower, Martinair 4133, flight level 200"))
    fly(-33.05, -60.45, 4000, toward=(saar.lat, saar.lon))
    tick(2)
    fly(-33.06, -60.76, 3000, hdg=270)
    tick(2)
    sim.update(wind_dir_deg=20.0, wind_kt=8.0)
    sim.place_on_final(saar, 5.0)
    tick(2)
    lat, lon = threshold(saar, saar.runways[0])
    sim.update(lat=lat + 0.002, lon=lon, on_ground=True, alt_msl_ft=85, alt_agl_ft=0, gs_kt=110, heading_deg=10)
    tick(1, 200)
    sim.update(gs_kt=50)
    tick(2)

    expected = ["Aeroparque Delivery, cleared to Rosario, ATOVO four bravo departure", "readback correct",
                "push and start approved", "taxi to holding point runway three one",
                "contact Aeroparque Tower one one eight decimal eight five", "runway three one, cleared for takeoff",
                "contact Aeroparque Approach one two zero decimal six", "radar contact, climb via SID",
                "contact Ezeiza Control one three five decimal five", "Ezeiza Control, radar contact",
                "contact Rosario Tower one one eight decimal seven", "descend to three thousand feet",
                "fly heading", "cleared approach runway zero two", "runway zero two, cleared to land",
                "welcome to Rosario"]
    i = 0
    for text in said:
        if i < len(expected) and expected[i] in text:
            i += 1
    assert i == len(expected), f"missing or out of order: {expected[i]!r}\n" + "\n".join(said)
    unexpected = ("give way", "hold position", "hold at", "say again", "negative", "read back", "Resistencia",
                  "squawk VFR", "turn left on", "turn right on", "unable")
    assert not [t for t in said if any(u in t for u in unexpected)], "\n".join(said)
    assert len(said) == len(expected)  # nothing else said to us
    assert _NoLLM.calls == 0
