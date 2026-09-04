"""
The vision witness: a cheap vision model a text-only main agent can interrogate
about an image it cannot see itself.

Deliberately not a captioner. A caption is written before anyone knows what will
be asked, so it has already thrown away the one detail the user cares about by
the time the question arrives. A witness still has the pixels when the question
comes, and can be asked again.

Kept provider-agnostic at the seams (`resolve` chooses who sees; `agent` runs the
loop) so it can lift into a shared `perception/` package when workflow nodes want
the same thing.
"""
from .agent import VISION_UNAVAILABLE, ask
from .resolve import Witness, resolve_witness, witness_available

__all__ = [
    "VISION_UNAVAILABLE",
    "Witness",
    "ask",
    "resolve_witness",
    "witness_available",
]
