"""One door to speech engines, whichever is configured.

Same shape as `sandbox/engine.py` and `browsing/engine.py`: `<ENGINE>=none`
(the default) means the tools are not offered at all, never offered and
refusing. Heavy work never runs on the app box — transcription and synthesis
are remote providers; what runs here is the file plumbing around them.
"""
