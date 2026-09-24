# `voice/`: speech to text and text to speech

Behind the `transcribe_audio` and `text_to_speech` tools (`chat/tools/voice.py`).
Both are **off by default**. Not a Django app: no models, no URLs.

| File | What it does |
|---|---|
| `stt.py` | Speech to text, chosen by `STT_ENGINE` |
| `tts.py` | Text to speech, chosen by `TTS_ENGINE` |
