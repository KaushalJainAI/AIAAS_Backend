"""
The witness prompt.

This text exists to fight one measured failure. On a chart whose labels were
tiny relative to the canvas, the model read `4.8` as `48` — silently, plausibly,
and identically on all three trials at temperature 0. It does not volunteer
doubt, so the prompt has to demand it: naming the smallness of text is the only
uncertainty signal that costs nothing.
"""
from __future__ import annotations

WITNESS_SYSTEM = """You are examining a file on behalf of another AI assistant \
that cannot see it. That assistant will act on what you say, and it has no way \
to check you. Answer as a careful witness, not as a helpful narrator.

Rules:

1. Report only what is actually visible. "I cannot tell from this image" is a \
correct and expected answer — it is far more useful than a confident guess.
2. Separate reading from inference. Say "the label reads" for text and numbers \
you can literally see; say "this appears to be" for anything you concluded. \
Never present an inference as a reading.
3. Flag small, blurred, pixelated or low-resolution text explicitly instead of \
reading through it. If a digit, a decimal point or a minus sign might be lost \
at this resolution, say so in the same sentence as the value. Decimal points are \
the specific thing that disappears.
4. Name what is occluded, cut off at an edge, or ambiguous.
5. Do not speculate about intent, authorship, or anything outside the frame.
6. Answer the question asked. Do not describe the whole image when one detail \
was requested — but do mention anything visible that contradicts the premise of \
the question.

Be concise and concrete. Prose, not markup."""


#: Appended to the answer when the parser and the witness read the same glyphs
#: differently. Two cheap models disagreeing is the only doubt signal available,
#: because neither volunteers doubt alone.
DISAGREEMENT_NOTE = (
    "\n\n[UNCERTAIN: a second model reading the same image transcribed it as: "
    "{parsed}. The two readings disagree, so treat any number or label above as "
    "unconfirmed and tell the user it is unclear rather than picking one.]"
)


def wants_verbatim(question: str) -> bool:
    """
    Whether this question turns on exact glyphs, and so deserves the cross-check.

    Deliberately generous: the parse call is ~1s and cheap, while the failure it
    catches is a wrong number delivered in a confident sentence. Missing a check
    costs more than running one that was not needed.
    """
    lowered = question.lower()
    return any(
        token in lowered
        for token in (
            "number", "value", "digit", "figure", "amount", "total", "sum",
            "percent", "%", "price", "cost", "date", "count", "how many",
            "how much", "label", "text", "say", "says", "written", "read",
            "word", "title", "caption", "name", "code", "id", "exact",
            "verbatim", "transcribe", "spell", "axis", "legend", "table",
        )
    )
