Piper voice files go here (<name>.onnx and <name>.onnx.json). They are git-ignored.

Download one (list all voices by running the module with no argument):
    python -m piper.download_voices en_US-lessac-medium --data-dir voices

Test it, with and without the radio effect:
    python tools/probe_voice.py voices/en_US-lessac-medium.onnx

Use it in the app:
    python -m atc --airport KTST --airports-dir tests/fixtures/airports --voice voices/en_US-lessac-medium.onnx

Piper now lives at github.com/OHF-Voice/piper1-gpl (GPL-3.0).
