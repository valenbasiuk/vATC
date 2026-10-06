"""Find your push-to-talk: lists game controllers, then for 20 s prints every joystick button and keyboard key
you press, in the form the app wants it.

    python tools/probe_ptt.py
    -> "device 0 (Honeycomb Alpha): button 3"   use:  --ptt-joy 3 --ptt-joy-device 0
    -> "key: f10"                               use:  --ptt-key f10

Also lists audio devices (for --mic / --audio-out if the Windows defaults are not your headset).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atc.audio import joystick  # noqa: E402


def main(seconds: float = 20.0) -> int:
    try:
        import sounddevice as sd  # type: ignore

        print("audio devices (index: name):")
        for i, d in enumerate(sd.query_devices()):
            kinds = ("in" if d["max_input_channels"] else "") + ("/out" if d["max_output_channels"] else "")
            print(f"  {i}: {d['name']} [{kinds.strip('/')}]")
        print(f"  default in/out: {sd.default.device}")
    except Exception as exc:  # noqa: BLE001
        print(f"(audio devices not listed: {exc})")
    devs = joystick.devices()
    print("game controllers:" if devs else "no game controllers found by Windows (WinMM)")
    for dev, name, n in devs:
        print(f"  device {dev}: {name} ({n} buttons)")
    try:
        from pynput import keyboard  # type: ignore

        def on_press(k):
            name = getattr(k, "name", None) or getattr(k, "char", None)
            print(f"key: {name}    use: --ptt-key {name}")

        keyboard.Listener(on_press=on_press, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"(keyboard not watched: {exc})")
    print(f"press buttons / keys now ({seconds:.0f} s) ...")
    last = {dev: 0 for dev, _, _ in devs}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for dev, name, _ in devs:
            mask = joystick.buttons(dev) or 0
            new = mask & ~last[dev]
            for b in joystick.pressed_list(new):
                print(f"device {dev} ({name}): button {b}    use: --ptt-joy {b} --ptt-joy-device {dev}")
            last[dev] = mask
        time.sleep(0.02)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
