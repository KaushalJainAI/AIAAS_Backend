"""
Media commands: `/image`, `/speak`, `/transcribe` (§18.6).

`/image` spends money per call and audio crosses modalities, so each says
plainly what happens next. No confirm sheets here: the turn itself has no
side effects, and the priced or outward tool still gates at dispatch through
the usual approval card — the user sees the rendered call (and its cost)
before anything runs, with the model having done the prep. `/speak` and
`/transcribe` hide behind their engines (`tts`/`stt`): listed only where
they can run, never offered-then-refusing.
"""
from __future__ import annotations

import logging

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="image",
    summary="Generate an image of this",
    args=[Arg("text", kind="text", required=False, hint="What to picture.")],
    kind="turn", group="media",
)
async def image_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    subject = str(call.args.get("text") or call.text or "").strip()
    if not subject:
        return CommandResult(
            status="error", message="Describe the picture — /image <what>."
        )
    return CommandResult(
        status="ok", args={"subject": subject},
        context_block=(
            f"[COMMAND /image]\nGenerate one image of: {subject} with "
            f"generate_image. One call — a generation is billed per call, so "
            f"never make variations unasked. Save it into the caller's write "
            f"folder and reply with the path. Approval at dispatch covers the "
            f"spend: the turn itself spends nothing."
        ),
        tool_pin=("generate_image",),
    )


@command(
    name="speak",
    summary="Read this out loud as audio",
    args=[Arg("text", kind="text", required=False, hint="What to say.")],
    kind="turn", group="media", requires="tts",
)
async def speak_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    text = str(call.args.get("text") or call.text or "").strip()
    if not text:
        return CommandResult(
            status="error", message="Say what — /speak <text>."
        )
    return CommandResult(
        status="ok", args={"text": text},
        context_block=(
            "[COMMAND /speak]\nSynthesise the text below into an audio file "
            "with text_to_speech, saved into the caller's write folder. Reply "
            "with the path.\n"
            f"TEXT:\n{text}"
        ),
        tool_pin=("text_to_speech",),
    )


@command(
    name="transcribe",
    summary="Transcribe an audio file I have",
    args=[Arg("file", kind="file", required=False,
              hint="The recording, by path.")],
    kind="turn", group="media", requires="stt",
)
async def transcribe_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    path = str(call.args.get("file") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"path": path} if path else {},
        context_block=(
            "[COMMAND /transcribe]\nTranscribe the recording with "
            "transcribe_audio"
            + (f": {path}" if path else " (ask which file when it is not clear)")
            + ". Return the transcript as text and offer to save it beside "
              "the recording."
        ),
        tool_pin=("transcribe_audio",),
    )
