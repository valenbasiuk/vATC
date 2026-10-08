"""The launcher (improvement plan I2/I3): set everything up in a window, start / stop the ATC, and watch the flight.

    pythonw -m atc.gui        (or vATC.bat in the project folder)

Setup tab: the flight (sim / fake sim, airport, SimBrief plan), the radio (voice + test, push-to-talk key or yoke
button found by pressing it, microphone / headset), the ATC (LLM providers with a key check, weather source, ATIS
question, standby), our own traffic (amount, liveries, tug). Saved to %APPDATA%\\vATC\\config.json (atc.config).
Flight tab: the transcript, a box to type calls (also with push-to-talk), quick buttons, and a live panel (the
station on COM1, the airport's frequencies, the ATIS, the runway, our traffic) from the status file the ATC process
writes. The ATC runs as its own process (`python -m atc ...` with the same options as by hand): if it stops, the
window doesn't.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from atc import config

ROOT = Path(__file__).resolve().parents[2]
STATUS = Path(tempfile.gettempdir()) / "vatc_status.json"
STT_MODELS = ["tiny.en", "base.en", "small.en", "medium.en"]
CREATE_NO_WINDOW = 0x08000000


def _voices() -> list[str]:
    return ["(text only)"] + sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in (ROOT / "voices").glob("*.onnx")
                                    if not p.name.startswith(("es_", "no_")))


def _audio_devices(kind: str) -> list[str]:
    """'index: name' of the input / output devices ('' first = Windows default)."""
    try:
        import sounddevice as sd

        out = [""]
        for i, d in enumerate(sd.query_devices()):
            if d[f"max_{kind}_channels"] > 0:
                out.append(f"{i}: {d['name']}")
        return out
    except Exception:  # noqa: BLE001 - no sounddevice: type it by hand
        return [""]


def _providers_status(models: str) -> str:
    from atc.llm.client import PROVIDERS, env_key

    used = []
    for part in models.split(","):
        name = part.split(":", 1)[0].strip()
        if name in PROVIDERS and name not in used:
            used.append(name)
    return "  ".join(f"{n} {'ok' if env_key(PROVIDERS[n][1]) else 'NO KEY'}" for n in used) or "no provider prefix"


def _line_tag(line: str) -> str:
    if line.startswith("YOU>"):
        return "you"
    if line.startswith("ATC>"):
        return "atc"
    if line.startswith("ATIS>"):
        return "atis"
    if line.startswith("[") or line.startswith("own traffic") or not line.strip():
        return "sys"
    if "> " in line[:60]:
        return "ai"
    return "sys"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("vATC")
        self.geometry("1100x760")
        self.minsize(900, 600)
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self.cfg = config.load()
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.v: dict[str, tk.Variable] = {}
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.setup = ttk.Frame(tabs, padding=10)
        self.flight = ttk.Frame(tabs, padding=6)
        tabs.add(self.setup, text="Setup")
        tabs.add(self.flight, text="Flight")
        self.tabs = tabs
        self._build_setup()
        self._build_flight()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(150, self._pump)
        self.after(1500, self._poll_status)

    # --- setup ---------------------------------------------------------------------------------------------------
    def _var(self, key: str, kind=tk.StringVar):
        val = getattr(self.cfg, key)
        var = kind(value="" if val is None else val)
        self.v[key] = var
        return var

    def _row(self, parent, r: int, label: str, widget, hint: str = "") -> None:
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
        widget.grid(row=r, column=1, sticky="we", pady=3)
        if hint:
            ttk.Label(parent, text=hint, foreground="#777").grid(row=r, column=2, sticky="w", padx=8)

    def _build_setup(self) -> None:
        f = self.setup
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        fl = ttk.LabelFrame(f, text="Flight", padding=8)
        fl.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        fl.columnconfigure(1, weight=1)
        self._row(fl, 0, "MSFS", ttk.Checkbutton(fl, text="real sim (off = fake sim)", variable=self._var("sim", tk.BooleanVar)))
        self._row(fl, 1, "Airport", ttk.Entry(fl, textvariable=self._var("airport"), width=10), "empty = where you are")
        sb = ttk.Frame(fl)
        ttk.Entry(sb, textvariable=self._var("simbrief_user"), width=18).pack(side="left")
        ttk.Button(sb, text="Fetch plan", command=self._fetch_plan).pack(side="left", padx=6)
        self._row(fl, 2, "SimBrief user", sb)
        self.plan_label = ttk.Label(fl, text=self._plan_summary(), foreground="#355")
        self.plan_label.grid(row=3, column=0, columnspan=3, sticky="w")
        self._row(fl, 4, "", ttk.Checkbutton(fl, text="fly the SimBrief plan", variable=self._var("use_simbrief", tk.BooleanVar)))
        self._row(fl, 5, "Callsign", ttk.Entry(fl, textvariable=self._var("callsign"), width=12), "empty = plan / sim")
        self._row(fl, 6, "Telephony", ttk.Entry(fl, textvariable=self._var("telephony"), width=16), 'e.g. "Argentina"')

        ra = ttk.LabelFrame(f, text="Radio", padding=8)
        ra.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        ra.columnconfigure(1, weight=1)
        voice = self._var("voice")
        if not voice.get():
            voice.set("(text only)")
        vb = ttk.Frame(ra)
        ttk.Combobox(vb, textvariable=voice, values=_voices(), width=34).pack(side="left")
        ttk.Button(vb, text="Test", command=self._test_voice).pack(side="left", padx=6)
        self._row(ra, 0, "Voice", vb)
        self._row(ra, 1, "Push-to-talk", ttk.Checkbutton(ra, text="talk (off = type only)", variable=self._var("ptt", tk.BooleanVar)))
        self._row(ra, 2, "PTT key", ttk.Entry(ra, textvariable=self._var("ptt_key"), width=8), 'f9, f10, v ... ("none" = yoke only)')
        jb = ttk.Frame(ra)
        self.joy_label = ttk.Label(jb, text=self._joy_text())
        self.joy_label.pack(side="left")
        ttk.Button(jb, text="Detect (press it)", command=self._detect_joy).pack(side="left", padx=6)
        ttk.Button(jb, text="Clear", command=self._clear_joy).pack(side="left")
        self._row(ra, 3, "Yoke button", jb)
        self._row(ra, 4, "Microphone", ttk.Combobox(ra, textvariable=self._var("mic"), values=_audio_devices("input"), width=34))
        self._row(ra, 5, "Headset", ttk.Combobox(ra, textvariable=self._var("audio_out"), values=_audio_devices("output"), width=34))
        self._row(ra, 6, "Speech model", ttk.Combobox(ra, textvariable=self._var("stt_model"), values=STT_MODELS, width=12),
                  "base.en if too slow")

        at = ttk.LabelFrame(f, text="ATC", padding=8)
        at.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        at.columnconfigure(1, weight=1)
        models = self._var("llm_models")
        self._row(at, 0, "LLM providers", ttk.Entry(at, textvariable=models, width=48))
        self.keys_label = ttk.Label(at, text=_providers_status(models.get()), foreground="#355")
        self.keys_label.grid(row=1, column=1, sticky="w")
        models.trace_add("write", lambda *_: self.keys_label.config(text=_providers_status(models.get())))
        wx = ttk.Frame(at)
        metar = self._var("metar")
        for val, txt in (("auto", "auto"), ("on", "live METAR"), ("off", "sim only")):
            ttk.Radiobutton(wx, text=txt, value=val, variable=metar).pack(side="left", padx=(0, 8))
        self._row(at, 2, "Weather", wx)
        self._row(at, 3, "ATIS", ttk.Checkbutton(at, text='ask "confirm information X" when not reported',
                                                 variable=self._var("atis_question", tk.BooleanVar)))
        self._row(at, 4, "Standby", ttk.Scale(at, from_=0.0, to=1.0, variable=self._var("standby", tk.DoubleVar)),
                  "how often Delivery says standby")

        tr = ttk.LabelFrame(f, text="Our own traffic", padding=8)
        tr.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)
        tr.columnconfigure(1, weight=1)
        self._row(tr, 0, "Traffic", ttk.Checkbutton(tr, text="on (set the sim's traffic and parked aircraft OFF)",
                                                    variable=self._var("own_traffic", tk.BooleanVar)))
        factor = self._var("own_factor", tk.DoubleVar)
        fb = ttk.Frame(tr)
        ttk.Scale(fb, from_=0.25, to=3.0, variable=factor, length=180).pack(side="left")
        flab = ttk.Label(fb, text=f"x{factor.get():.2f}")
        flab.pack(side="left", padx=6)
        factor.trace_add("write", lambda *_: flab.config(text=f"x{factor.get():.2f}"))
        self._row(tr, 1, "Amount", fb, "x the airport's usual")
        self._row(tr, 2, "Max aircraft", ttk.Spinbox(tr, from_=0, to=20, textvariable=self._var("own_max", tk.StringVar), width=5),
                  "0 or empty = by airport")
        self._row(tr, 3, "Liveries", ttk.Combobox(tr, textvariable=self._var("own_models"), values=["fstraffic", "fsltl"], width=12))
        self._row(tr, 4, "Pushback tug", ttk.Checkbutton(tr, text="GSX tug under the nose", variable=self._var("own_tug", tk.BooleanVar)))

        bt = ttk.Frame(f)
        bt.grid(row=2, column=0, columnspan=2, sticky="e", pady=10)
        self.status_label = ttk.Label(bt, text="stopped", foreground="#777")
        self.status_label.pack(side="left", padx=10)
        ttk.Button(bt, text="Save", command=self._save).pack(side="left", padx=4)
        self.start_btn = ttk.Button(bt, text="Start ATC", command=self._start)
        self.start_btn.pack(side="left", padx=4)
        ttk.Button(bt, text="Stop", command=self._stop).pack(side="left", padx=4)

    def _read_cfg(self) -> config.Config:
        c = self.cfg
        for key, var in self.v.items():
            val = var.get()
            if key in ("ptt_joy", "ptt_joy_device"):
                continue
            if key == "own_max":
                val = int(val) if str(val).strip().isdigit() and int(val) > 0 else None
            if key == "voice" and val == "(text only)":
                val = ""
            if key in ("mic", "audio_out") and isinstance(val, str) and ":" in val:
                val = val.split(":", 1)[0].strip()  # "3: Headset" -> "3"
            setattr(c, key, val)
        return c

    def _save(self) -> None:
        p = config.save(self._read_cfg())
        self.status_label.config(text=f"saved {p}")

    def _plan_summary(self) -> str:
        p = ROOT / "simbrief_last.json"
        if not p.exists():
            return "no plan fetched"
        try:
            from atc.flightplan import load_simbrief

            fp = load_simbrief(p)
            return (f"plan: {fp.callsign} {fp.origin}-{fp.destination} {fp.aircraft_type} "
                    f"{fp.sid or ''} FL{(fp.cruise_ft or 0) // 100} squawk {fp.squawk}")
        except Exception as exc:  # noqa: BLE001
            return f"plan file unreadable: {exc}"

    def _fetch_plan(self) -> None:
        user = self.v["simbrief_user"].get().strip()
        if not user:
            messagebox.showinfo("vATC", "Type your SimBrief username first.")
            return

        def work() -> None:
            import urllib.parse
            import urllib.request

            url = "https://www.simbrief.com/api/xml.fetcher.php?" + urllib.parse.urlencode({"username": user, "json": "v2"})
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.load(resp)
                (ROOT / "simbrief_last.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
                self.after(0, lambda: (self.plan_label.config(text=self._plan_summary()),
                                       self.v["use_simbrief"].set(True)))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self.plan_label.config(text=f"SimBrief: {exc}"))

        self.plan_label.config(text="fetching ...")
        threading.Thread(target=work, daemon=True).start()

    def _joy_text(self) -> str:
        if self.cfg.ptt_joy is None:
            return "none"
        return f"button {self.cfg.ptt_joy}" + (f" on device {self.cfg.ptt_joy_device}" if self.cfg.ptt_joy_device is not None else "")

    def _clear_joy(self) -> None:
        self.cfg.ptt_joy = self.cfg.ptt_joy_device = None
        self.joy_label.config(text=self._joy_text())

    def _detect_joy(self) -> None:
        """Press the yoke button within 10 s: the first button that goes down (not one that is always held, like a
        switch) becomes the push-to-talk."""
        def work() -> None:
            import time

            try:
                from atc.audio import joystick

                devs = [d[0] for d in joystick.devices()]
                base = {d: joystick.buttons(d) or 0 for d in devs}
                end = time.time() + 10
                while time.time() < end:
                    for d in devs:
                        now = joystick.buttons(d) or 0
                        new = now & ~base[d]
                        if new:
                            b = joystick.pressed_list(new)[0]
                            self.cfg.ptt_joy, self.cfg.ptt_joy_device = b, d
                            self.after(0, lambda: self.joy_label.config(text=self._joy_text()))
                            return
                    time.sleep(0.03)
                self.after(0, lambda: self.joy_label.config(text="nothing pressed"))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self.joy_label.config(text=f"no joystick: {exc}"))

        self.joy_label.config(text="press the button now ...")
        threading.Thread(target=work, daemon=True).start()

    def _test_voice(self) -> None:
        voice = self.v["voice"].get()
        if voice in ("", "(text only)"):
            return
        out = self.v["audio_out"].get()
        out = out.split(":", 1)[0].strip() if ":" in out else out

        def work() -> None:
            try:
                from atc.audio.tts import PiperTTS

                dev = int(out) if out.isdigit() else (out or None)
                PiperTTS(ROOT / voice, device=dev).say("Aeroparque Tower, wind one three zero degrees eight knots, "
                                                       "runway one three, cleared for takeoff.")
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: messagebox.showerror("vATC", f"Voice test failed: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    # --- the ATC process -------------------------------------------------------------------------------------------
    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        cfg = self._read_cfg()
        config.save(cfg)
        if not cfg.sim and not cfg.airport.strip():
            messagebox.showinfo("vATC", "With the fake sim, give an airport (e.g. SABE).")
            return
        try:
            STATUS.unlink()
        except OSError:
            pass
        argv = [sys.executable.replace("pythonw.exe", "python.exe"), "-u", "-m", "atc"] + cfg.to_argv(str(STATUS))
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", **cfg.env())
        self.proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                     creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.lines.put("[started: python -m atc " + " ".join(argv[4:]) + "]")
        self.status_label.config(text="running", foreground="#2a2")
        self.tabs.select(self.flight)

    def _reader(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(f"[ATC stopped (exit {proc.wait()})]")

    def _send(self, text: str) -> None:
        if self.proc is None or self.proc.poll() is not None or not text.strip():
            return
        try:
            self.proc.stdin.write(text.strip() + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self._send("/quit")
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        self.status_label.config(text="stopped", foreground="#777")

    def _close(self) -> None:
        self._stop()
        self.destroy()

    # --- flight tab ------------------------------------------------------------------------------------------------
    def _build_flight(self) -> None:
        f = self.flight
        f.columnconfigure(0, weight=3)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(0, weight=1)
        left = ttk.Frame(f)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(left, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        for tag, color in (("you", "#1b5fbf"), ("atc", "#16803c"), ("atis", "#7a5a00"), ("ai", "#555555"),
                           ("sys", "#999999")):
            self.log.tag_config(tag, foreground=color)
        row = ttk.Frame(left)
        row.grid(row=1, column=0, sticky="we", pady=4)
        row.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(row)
        self.entry.grid(row=0, column=0, sticky="we")
        self.entry.bind("<Return>", lambda _e: self._send_entry())
        ttk.Button(row, text="Send", command=self._send_entry).grid(row=0, column=1, padx=4)
        quick = ttk.Frame(left)
        quick.grid(row=2, column=0, sticky="w")
        for label, cmd in (("Departure of ours", "/owndep"), ("Arrival of ours", "/ownarr"), ("GA arrival", "/ownarr ga"),
                           ("Say again", "say again")):
            ttk.Button(quick, text=label, command=lambda c=cmd: self._send(c)).pack(side="left", padx=(0, 4))

        right = ttk.Frame(f)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        self.station = ttk.Label(right, text="-", font=("Segoe UI", 12, "bold"))
        self.station.grid(row=0, column=0, sticky="w")
        self.where = ttk.Label(right, text="")
        self.where.grid(row=1, column=0, sticky="w")
        ttk.Label(right, text="Frequencies").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.freqs = tk.Listbox(right, height=7, font=("Consolas", 10))
        self.freqs.grid(row=3, column=0, sticky="we")
        ttk.Label(right, text="ATIS").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.atis = tk.Message(right, text="-", width=380, font=("Segoe UI", 9))
        self.atis.grid(row=5, column=0, sticky="we")
        ttk.Label(right, text="Our traffic").grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.own = ttk.Treeview(right, columns=("type", "kind", "state"), height=10)
        self.own.heading("#0", text="callsign")
        for c, w in (("type", 60), ("kind", 80), ("state", 120)):
            self.own.heading(c, text=c)
            self.own.column(c, width=w)
        self.own.column("#0", width=90)
        self.own.grid(row=7, column=0, sticky="nsew")
        right.rowconfigure(7, weight=1)

    def _send_entry(self) -> None:
        text = self.entry.get()
        self.entry.delete(0, "end")
        self._send(text)

    def _pump(self) -> None:
        added = False
        try:
            for _ in range(200):
                line = self.lines.get_nowait()
                if line.strip() in ("YOU>", ""):
                    continue
                line = line.replace("YOU> YOU> ", "YOU> ")
                self.log.config(state="normal")
                self.log.insert("end", line + "\n", _line_tag(line))
                added = True
        except queue.Empty:
            pass
        if added:
            self.log.see("end")
            self.log.config(state="disabled")
        if self.proc is not None and self.proc.poll() is not None and self.status_label.cget("text") == "running":
            self.status_label.config(text="stopped", foreground="#777")
        self.after(150, self._pump)

    def _poll_status(self) -> None:
        try:
            data = json.loads(STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data:
            st = data.get("station")
            self.station.config(text=f"{st['name']}  ({data['com1']:.3f})" if st else f"COM1 {data['com1']:.3f}: nobody")
            apt = data.get("airport") or {}
            wx = "  sim weather" if data.get("sim_weather") else ""
            self.where.config(text=f"{apt.get('icao', '')} {apt.get('name', '')}   runway {data.get('runway') or '-'}"
                                   f"   {data['callsign']}{wx}")
            self.freqs.delete(0, "end")
            for fr in apt.get("frequencies", []):
                mark = "<" if abs(fr["mhz"] - data["com1"]) < 0.005 else " "
                self.freqs.insert("end", f"{fr['kind']:5s} {fr['mhz']:8.3f} {mark}")
            a = data.get("atis")
            self.atis.config(text=f"Information {a['letter']}: {a['text']}" if a else "no ATIS here (the controller gives the weather)")
            self.own.delete(*self.own.get_children())
            for o in data.get("own", []):
                self.own.insert("", "end", text=o["callsign"], values=(o["type"], o["kind"], o["state"]))
        self.after(1500, self._poll_status)


def main() -> None:
    os.chdir(ROOT)
    App().mainloop()


if __name__ == "__main__":
    main()
