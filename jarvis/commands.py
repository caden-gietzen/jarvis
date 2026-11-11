"""Spoken commands Jarvis handles itself, without a round trip to Claude.

  "switch to the Ryan voice"      -> change the speaking voice
  "switch to Sonnet" / "use Opus" -> change the Claude model for the rest of the session
"""

import re

from . import restart
from .speaker import find_voice, list_voices, nickname

# Claude Code aliases: each resolves to the newest model in that family.
MODELS = ("opus", "sonnet", "haiku", "fable")

_SWITCH = re.compile(r"\b(switch|change|use|try|swap|go)\b", re.I)
_VOICE = re.compile(r"\bvoices?\b", re.I)
_MODEL_WORD = re.compile(r"\bmodels?\b", re.I)
_MODEL_NAME = re.compile(r"\b(" + "|".join(MODELS) + r")\b", re.I)
_MAX_WORDS = 12  # longer sentences are real requests for Claude, not commands
_MAX_VAGUE_WORDS = 6  # "change your voice" with no name: only when that's all you said


_RESTART = re.compile(r"\brestart\b(\s+(jarvis|the session|yourself|the app))?\W*$", re.I)
RESTART_REPLY = "Understood. I'll restart as soon as I finish this reply."


async def handle(text: str, speaker, brain) -> str | None:
    """Return Jarvis's reply if text was a local command, else None."""
    if speaker is not None and len(text.split()) <= _MAX_VAGUE_WORDS and _RESTART.search(text.strip()):
        restart.request_restart()
        return RESTART_REPLY
    if len(text.split()) > _MAX_WORDS or not _SWITCH.search(text):
        return None

    if _VOICE.search(text):
        voice = find_voice(text)
        if voice and speaker is None:
            return "Voices only apply when I'm talking; use `run` mode."
        if voice and speaker.set_voice(voice):
            return f"Okay, this is the {nickname(voice)} voice."
        if speaker is None or len(text.split()) > _MAX_VAGUE_WORDS:
            return None
        return f"I have these voices: {', '.join(nickname(v) for v in list_voices())}. Which one would you like?"

    name = _MODEL_NAME.search(text)
    if name:
        await brain.set_model(name.group(1).lower())
        return f"Okay, switching to {name.group(1).capitalize()}. The next reply will show the model it actually used."
    if _MODEL_WORD.search(text) and len(text.split()) <= _MAX_VAGUE_WORDS:
        return f"I can switch to {', '.join(m.capitalize() for m in MODELS)}. Which one?"
    return None
