"""
Untrusted content coming in, untrusted URLs going out.

`security` is prompt-injection detection and input sanitisation; `net` is the
single SSRF-checked fetch every user-, agent- or config-chosen URL goes through.
One package because they are the same question asked at opposite ends: this
process must not be steered by data it was handed.
"""
