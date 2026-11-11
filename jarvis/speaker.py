"""Text to speech (Piper, or the built-in Windows voice) with interruptible playback."""

import contextvars
import json
import math
import queue
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from . import ui
from .config import ROOT, STATE_PATH
from .events import bus
from .state_file import read_state, update_state

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# The wake model keys on the phrase "hey Jarvis"; Jarvis must never say it (always-on wake word).
_WAKE_PHRASE = re.compile(r"\b(?:hey|hay|hi|hello|ok|okay)[\s,.!?_-]*jarvis\b", re.IGNORECASE)
WAKE_SUBSTITUTE = "the wake word"


def no_wake_phrase(text: str) -> str:
    """Rewrite any "hey Jarvis" variant so spoken audio can't trigger the wake word."""
    return _WAKE_PHRASE.sub(WAKE_SUBSTITUTE, text)


def for_speech(text: str) -> str:
    """Markdown reply -> something sensible to read aloud. The terminal has the full text."""
    text = _CODE_BLOCK.sub(" I've put the code in the terminal. ", text)
    text = _LINK.sub(r"\1", text)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|"):  # table rows
            continue
        s = re.sub(r"^(#+|[-*+]|\d+[.)])\s+", "", s)
        if s and s[-1] not in ".!?:;,":
            s += "."  # headings and list items need a pause when spoken
        lines.append(s)
    text = " ".join(l for l in lines if l)
    text = re.sub(r"[*_`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


SPEAK_MARGIN_S = 60.0  # flat slack on top of the scaled estimate (model warm-up, slow devices)
SPEAK_SCALE = 2.0  # the estimated audio length is multiplied by this: the margin grows with the reply
SPEAK_MAX_S = 0.0  # optional absolute ceiling on one speak(); 0 = none (the deadline just scales with length)
SPEAK_GRACE_S = 15.0  # past the speaker's own deadline before a caller stops waiting on speak() (tts.speak_grace_s)
MIN_SPEAK_MARGIN_S = 10.0  # configured margins below this are raised to it (a ~zero deadline would abandon every reply)
MIN_SPEAK_GRACE_S = 1.0
SPEAK_CHARS_PER_S = 8.0  # conservative reading speed used for that estimate (real speech is ~2x faster)
CHUNK_GET_TIMEOUT_S = 30.0  # longest we wait for the next synthesized sentence
OPEN_TIMEOUT_S = 5.0  # longest we wait for the output device to open/start
CHUNK_MARGIN_S = 5.0  # slack on one chunk's playback time before declaring the stream hung
_FAILED = object()  # producer -> consumer: synthesis raised

VOICE_DIR = ROOT / "voices"


def list_voices() -> list[str]:
    return sorted(p.stem for p in VOICE_DIR.glob("*.onnx"))


def nickname(voice: str) -> str:
    """en_GB-northern_english_male-medium -> northern english male"""
    return voice.split("-")[1].replace("_", " ")


def find_voice(words: str) -> str | None:
    """Match spoken words like 'ryan' or 'northern' to an installed voice."""
    words = words.lower()
    for v in list_voices():
        if any(part in words for part in nickname(v).split() if part not in ("english", "male")):
            return v
    return None


class _Piper:
    def __init__(self, cfg: dict):
        from piper import SynthesisConfig

        self.syn = SynthesisConfig(length_scale=cfg["length_scale"], volume=cfg["volume"])
        self._loaded = {}
        self.set_voice(cfg["voice"])

    def set_voice(self, name: str):
        from piper import PiperVoice

        if name not in self._loaded:
            self._loaded[name] = PiperVoice.load(str(VOICE_DIR / f"{name}.onnx"))
        self.name = name
        self.voice = self._loaded[name]

    def chunks(self, text: str):
        for c in self.voice.synthesize(text, self.syn):
            yield c.audio_float_array, c.sample_rate


class _Sapi:
    """Windows' built-in voice via PowerShell. Robotic, but always there."""

    def chunks(self, text: str):
        for sentence in _SENTENCE.split(text):
            with tempfile.TemporaryDirectory() as d:
                wav = Path(d) / "s.wav"
                ps = (
                    "Add-Type -AssemblyName System.Speech;"
                    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    f"$s.SetOutputToWaveFile('{wav}');"
                    "$s.Speak([Console]::In.ReadToEnd());$s.Dispose()"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps], input=sentence, text=True,
                               capture_output=True, check=True)
                with wave.open(str(wav)) as w:
                    a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                    yield a.astype(np.float32) / 32768.0, w.getframerate()


PERMISSION_SPEECH: contextvars.ContextVar = contextvars.ContextVar("jarvis_permission_speech", default=False)
# Set (by Brain._say) around a spoken permission prompt; asyncio.to_thread copies it into the speak() thread.

_MUTE_PHRASES = ("mute|voice off|go quiet|be quiet|mute voice|turn off your voice")
_UNMUTE_PHRASES = ("un-?mute(?: voice| your voice| yourself)?|voice on|turn on your voice|you can talk again")
_VOICE_CMD = re.compile(r"^(?:jarvis\s+)?(?:please\s+)?(?:(?P<mute>" + _MUTE_PHRASES + r")|(?P<unmute>" + _UNMUTE_PHRASES
                        + r"))(?:\s+please)?$")


def voice_command(text: str) -> str | None:
    """"mute" / "unmute" if the whole utterance is one of the exact short phrases (case and trailing
    punctuation ignored), else None. A normal sentence that contains the word never matches."""
    t = re.sub(r"[^\w\s-]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    m = _VOICE_CMD.match(t)
    if not m:
        return None
    return "mute" if m.group("mute") else "unmute"


_CONV_ON = r"(?:conversation\s+mode\s+on|(?:start|enter|begin|turn\s+on)\s+conversation\s+mode)"
_CONV_OFF = r"(?:conversation\s+mode\s+off|(?:stop|exit|end|leave|turn\s+off)\s+conversation\s+mode)"
_CONV_CMD = re.compile(r"^(?:jarvis\s+)?(?:please\s+)?(?:(?P<on>" + _CONV_ON + r")|(?P<off>" + _CONV_OFF + r"))(?:\s+please)?$")


def conversation_command(text: str) -> str | None:
    """"on" / "off" if the whole utterance is one of the exact short conversation-mode phrases (case and
    punctuation ignored), else None. A sentence that merely contains the words never matches."""
    t = re.sub(r"[^\w\s-]", " ", (text or "").lower())
    t = re.sub(r"\s+", " ", t).strip()
    m = _CONV_CMD.match(t)
    if not m:
        return None
    return "on" if m.group("on") else "off"


def load_conversation_mode(path: Path | None = None) -> bool:
    """The persisted conversation-mode flag (.jarvis_state.json "conversation_mode"). True (the default)
    if absent, not a boolean, or unreadable: only an explicit saved false turns it off."""
    try:
        return read_state(Path(path or STATE_PATH)).get("conversation_mode") is not False
    except (OSError, ValueError, AttributeError):
        return True


def save_conversation_mode(on: bool, path: Path | None = None) -> None:
    """Write the flag into the state file, keeping its other keys. Never raises."""
    p = Path(path or STATE_PATH)
    try:
        update_state(p, conversation_mode=bool(on))
    except OSError as e:
        ui.error(f"Couldn't save the conversation mode setting ({type(e).__name__}).")


def load_muted(path: Path | None = None) -> bool:
    """The persisted mute flag (.jarvis_state.json "voice_muted"); False if absent or unreadable."""
    try:
        return read_state(Path(path or STATE_PATH)).get("voice_muted") is True
    except (OSError, ValueError, AttributeError):
        return False


def save_muted(muted: bool, path: Path | None = None) -> None:
    """Write the flag into the state file, keeping its other keys. Never raises."""
    p = Path(path or STATE_PATH)
    try:
        update_state(p, voice_muted=bool(muted))
    except OSError as e:
        ui.error(f"Couldn't save the voice on/off setting ({type(e).__name__}).")


def lenient_seconds(value, default, name, minimum=None):
    """Parse a configured number of seconds without ever raising. None / missing -> default silently;
    non-numeric or non-finite (x, NaN, inf) -> default with ONE warning line. A finite value below
    `minimum` is raised to it."""
    if value is None:
        return default
    try:
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(value)
    except (TypeError, ValueError, OverflowError):
        ui.activity(f"Ignoring invalid tts.{name} ({value!r}); using the default.")
        return default
    if minimum is not None and v < minimum:
        ui.activity(f"tts.{name} {v:g} is too small; using {minimum:g} s.")
        return minimum
    return v


class Speaker:
    dictating = False  # set while a dictation is recording the mic: nothing may be spoken into it

    def __init__(self, cfg: dict):
        tts = cfg["tts"]
        self.chime_on = tts["chime"]
        # Hang-protection timing (see deadline_for). Absent keys fall back to the module defaults.
        self.speak_margin_s = lenient_seconds(tts.get("speak_margin_s"), None, "speak_margin_s", MIN_SPEAK_MARGIN_S)
        self.speak_max_s = lenient_seconds(tts.get("speak_max_s"), None, "speak_max_s", 0.0)
        self.muted_speaks_permission_prompts = bool(cfg.get("muted_speaks_permission_prompts", True))
        self.muted = load_muted()
        self._live: set = set()  # stop flags of speech in progress (not permission prompts), cut on mute
        self._live_lock = threading.Lock()
        self.engine = {"piper": lambda: _Piper(tts["piper"]), "sapi": _Sapi, "none": lambda: None}[tts["engine"]]()

    def silenced(self, permission: bool = False) -> bool:
        """True if speech of this kind must not be spoken right now (voice is off, or the mic is busy
        with a dictation, which silences even permission prompts: answer those on the page)."""
        if self.dictating:
            return True
        return self.muted and not (permission and self.muted_speaks_permission_prompts)

    def set_muted(self, muted: bool, persist: bool = True) -> None:
        """Voice on/off. Muting also cuts off whatever is being spoken right now. Thread-safe."""
        muted = bool(muted)
        self.muted = muted
        if persist:
            save_muted(muted)
        if muted:
            with self._live_lock:
                live = list(self._live)
            for stop in live:
                stop.set()
        bus.emit("voice", muted=muted)

    @property
    def voice(self) -> str | None:
        return getattr(self.engine, "name", None)

    def set_voice(self, name: str) -> bool:
        if not isinstance(self.engine, _Piper) or name not in list_voices():
            return False
        self.engine.set_voice(name)
        return True

    def deadline_for(self, text: str) -> float:
        """Hang-protection deadline for speak(): it grows with the reply, so a long reply is never cut
        short by a fixed cap. margin + scale * (a deliberately slow reading-time estimate); optionally
        bounded by speak_max_s (0 = no ceiling)."""
        margin = getattr(self, "speak_margin_s", None)
        margin = SPEAK_MARGIN_S if margin is None else margin
        mx = getattr(self, "speak_max_s", None)
        mx = SPEAK_MAX_S if mx is None else mx
        d = margin + SPEAK_SCALE * len(text) / SPEAK_CHARS_PER_S
        return min(d, mx) if mx > 0 else d

    def _play(self, audio: np.ndarray, sr: int, stop: threading.Event, deadline: float) -> str:
        """Play on a stream of our own (never sd.play's shared global one, so a chime and
        speech can't cut each other off). Returns "done", "stopped" or "timeout"."""
        audio = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        pos = 0
        finished = threading.Event()

        def callback(outdata, frames, time_info, status):
            nonlocal pos
            n = min(frames, len(audio) - pos)
            outdata[:n, 0] = audio[pos:pos + n]
            outdata[n:] = 0
            pos += n
            if pos >= len(audio):
                raise sd.CallbackStop

        # Opening/starting a stream can itself block on a wedged driver, so do it on a
        # helper thread we can abandon. An abandoned open disposes its stream if it ever returns.
        box: dict = {}
        opened = threading.Event()
        abandoned = threading.Event()

        def dispose(st):
            # abort/close can block on a wedged driver; never let that block speech.
            def work():
                try:
                    st.abort()
                    st.close()
                except Exception:
                    pass

            threading.Thread(target=work, daemon=True).start()

        def open_stream():
            try:
                st = sd.OutputStream(samplerate=sr, channels=1, dtype="float32",
                                     callback=callback, finished_callback=finished.set)
                box["stream"] = st
                st.start()
            except Exception as e:
                box["error"] = e
            finally:
                opened.set()
                if abandoned.is_set() and "stream" in box:
                    dispose(box["stream"])

        threading.Thread(target=open_stream, daemon=True).start()
        try:
            open_end = min(deadline, time.monotonic() + OPEN_TIMEOUT_S)
            while not opened.wait(0.03):
                if stop.is_set():
                    return "stopped"
                if time.monotonic() >= open_end:
                    ui.error("Opening the audio output stream hung; abandoning it.")
                    return "timeout"
            if "error" in box:
                raise box["error"]
            end = min(deadline, time.monotonic() + len(audio) / sr + CHUNK_MARGIN_S)
            while not finished.wait(0.03):
                if stop.is_set():
                    return "stopped"
                if time.monotonic() >= end:
                    return "timeout"
            return "done"
        finally:
            abandoned.set()
            if "stream" in box:
                dispose(box["stream"])

    def chime(self):
        """Short rising tone: 'I'm listening'. Fire and forget on its own thread and stream,
        so it neither blocks the event loop nor interrupts speech."""
        if not self.chime_on or self.dictating:
            return
        sr = 22050
        t = np.arange(int(sr * 0.07)) / sr
        tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)])
        tone *= np.hanning(len(tone)) * 0.25

        def play():
            try:
                self._play(tone, sr, threading.Event(), time.monotonic() + 3)
            except Exception as e:
                ui.error(f"Chime failed ({e}).")

        threading.Thread(target=play, daemon=True).start()

    def speak(self, text: str, stop: threading.Event) -> bool:
        """Say text, sentence by sentence. Returns False if stop was set part way through
        or speech failed or timed out. Never blocks past deadline_for(text)."""
        if self.engine is None or not text:
            return True
        permission = PERMISSION_SPEECH.get()
        if self.silenced(permission):
            return True  # voice is off: the caller has already printed the text
        if not permission:
            with self._live_lock:
                self._live.add(stop)
            try:
                if self.silenced(False):  # muted between the check above and registering
                    return True
                return self._speak(text, stop)
            finally:
                with self._live_lock:
                    self._live.discard(stop)
        return self._speak(text, stop)

    def _speak(self, text: str, stop: threading.Event) -> bool:
        text = no_wake_phrase(text)  # audio only: the terminal and logs keep the original
        deadline = time.monotonic() + self.deadline_for(text)
        chunks: queue.Queue = queue.Queue(maxsize=4)
        abandon = threading.Event()  # tells the producer to quit if we give up early

        def put(item):
            while not (stop.is_set() or abandon.is_set()):
                try:
                    chunks.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    pass
            return False

        def produce():
            try:
                for item in self.engine.chunks(text):
                    if not put(item):
                        return
            except Exception as e:
                ui.error(f"Speech synthesis failed ({type(e).__name__}: {e}).")
                put(_FAILED)
                return
            put(None)

        threading.Thread(target=produce, daemon=True).start()
        try:
            while True:
                try:
                    item = chunks.get(timeout=max(0.05, min(CHUNK_GET_TIMEOUT_S, deadline - time.monotonic())))
                except queue.Empty:
                    if stop.is_set():
                        return False
                    ui.error("Speech synthesis produced nothing in time; giving up on this reply.")
                    return False
                if item is None:
                    return not stop.is_set()
                if item is _FAILED:
                    return False
                audio, sr = item
                try:
                    result = self._play(audio, sr, stop, deadline)
                except Exception as e:  # e.g. the output device disappeared: stay text-only
                    ui.error(f"Audio playback failed ({type(e).__name__}: {e}); skipping speech for this reply.")
                    return False
                if result == "stopped":
                    return False
                if result == "timeout":
                    ui.error("Audio playback hung or ran past its deadline; stopped speaking.")
                    return False
                if time.monotonic() >= deadline:
                    ui.error("Speech ran past its deadline; stopped.")
                    return False
        finally:
            abandon.set()
