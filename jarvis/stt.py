"""Speech to text: local faster-whisper, or Groq's hosted Whisper."""

import io
import os
import re
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from . import ui
from .config import LOG_DIR

# Whisper often keeps the tail of the wake phrase ("Jarvis, what's...") or
# hallucinates filler on near-silence.
_WAKE_PREFIX = re.compile(r"^\s*(hey[,\s]+)?jarvis[,.!?\s]*", re.IGNORECASE)
_JUNK = {"", "you", "thank you.", "thanks for watching!", "."}

# Model names Whisper tends to mishear. Fixed up here so commands.py can match "Sonnet".
_MODEL_FIXES = [
    (re.compile(r"\bson+e?t\b", re.IGNORECASE), "Sonnet"),  # "sonet", "sonnet", "sonnett"
]
# Biases Whisper's decoder toward these words (initial prompt for local transcription).
VOCAB_PROMPT = "Jarvis. Switch to Sonnet, Opus, Haiku, or Fable."


def clean(text: str) -> str:
    text = _WAKE_PREFIX.sub("", text.strip()).strip()
    for pattern, replacement in _MODEL_FIXES:
        text = pattern.sub(replacement, text)
    return "" if text.lower() in _JUNK else text


class LocalWhisper:
    def __init__(self, cfg: dict):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            cfg["model"], device="cpu", compute_type=cfg["compute_type"], cpu_threads=cfg["cpu_threads"],
        )

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False,
            initial_prompt=VOCAB_PROMPT,
        )
        return " ".join(s.text.strip() for s in segments)


class GroqWhisper:
    URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(self, cfg: dict):
        self.model = cfg["model"]
        key_env = cfg["api_key_env"]
        self.key = os.environ.get(key_env)
        if not self.key:
            raise RuntimeError(f"stt.engine is groq but ${key_env} is not set")
        self.http = httpx.Client(timeout=30)

    def transcribe(self, audio: np.ndarray) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        r = self.http.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.key}"},
            files={"file": ("speech.wav", buf.getvalue(), "audio/wav")},
            data={"model": self.model, "language": "en", "response_format": "json"},
        )
        r.raise_for_status()
        return r.json()["text"]


def make_stt(cfg: dict):
    engine = cfg["stt"]["engine"]
    if engine == "local":
        return LocalWhisper(cfg["stt"]["local"])
    if engine == "groq":
        return GroqWhisper(cfg["stt"]["groq"])
    raise ValueError(f"unknown stt.engine: {engine}")


def save_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def make_transcriber(stt):
    """Wrap an STT engine as transcribe(audio) -> cleaned text ('' if nothing recognisable)."""

    def transcribe(audio):
        save_wav(LOG_DIR / "last_utterance.wav", audio)
        peak = float(np.abs(audio).max())
        if 0 < peak < 0.5:
            audio = audio * (0.9 / peak)  # quiet mics: Whisper does better near full scale
        t0 = time.perf_counter()
        raw = stt.transcribe(audio)
        text = clean(raw)
        ui.activity(
            f"{len(audio) / 16000:.1f}s of audio, peak level {peak:.3f}, "
            f"transcribed in {time.perf_counter() - t0:.1f}s"
        )
        if not text:
            ui.activity(f"Whisper heard: {raw!r} (saved to logs/last_utterance.wav)")
        return text

    return transcribe
