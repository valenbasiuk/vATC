"""Speech-to-text with faster-whisper. STATUS: unverified draft (Phase 3)."""

from __future__ import annotations

PROMPT_EN = ("Air traffic control radio. Request IFR clearance, with information, Delivery, Ground, Tower, request "
             "push and start, ready to copy. Runway, taxi, hold short, cleared for takeoff, downwind, base, final, touch and "
             "go, full stop, left turnout, squawk, QNH, altimeter, readback, holding short, progressive taxi, flight "
             "following, request holding, radio check, say again, Cessna, niner, tree, fife. ")


class FasterWhisperSTT:
    def __init__(self, model_size: str = "small.en", device: str = "cpu", compute_type: str = "int8",
                 language: str = "en") -> None:
        # CPU on purpose: the GPU is busy with MSFS. VERIFY speed with the model size you pick.
        from faster_whisper import WhisperModel  # type: ignore

        if language != "en" and model_size.endswith(".en"):  # the .en models only know English
            model_size = model_size[:-3]
        self.language = language
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def transcribe(self, audio_f32_16k, hint: str = "", hotwords: str | None = None) -> str:
        """audio_f32_16k: float32 numpy array, mono, 16 kHz. `hint`: names for this flight (station,
        telephony, SID, destination), so "Martinair" isn't heard as "Martin Air". `hotwords`: the same names as
        a short list, which faster-whisper weights more strongly than the prompt."""
        if self.language == "es":
            from atc.spanish import STT_PROMPT

            prompt = STT_PROMPT + " " + hint
        else:
            prompt = PROMPT_EN + hint  # biasing toward aviation vocabulary helps a lot with callsigns and phraseology
        segments, _info = self._model.transcribe(
            audio_f32_16k,
            language=self.language,
            beam_size=1,
            vad_filter=True,
            without_timestamps=True,  # one short call: no timestamps, no conditioning on earlier text (faster)
            condition_on_previous_text=False,
            temperature=0.0,
            hotwords=hotwords or None,
            initial_prompt=prompt,
        )
        return " ".join(s.text.strip() for s in segments).strip()
